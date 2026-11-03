"""Authenticated local backups. Wallet keys are copied encrypted, never decrypted.

Version 1: fixed magic/version, 16-byte salt, 12-byte nonce, AES-256-GCM
ciphertext and a 16-byte tag. The header is authenticated; scrypt parameters
are fixed by the format, never supplied by an untrusted archive.
"""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
import zlib
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import scrypt
from Crypto.Random import get_random_bytes

from .diagnostics import record_operation, sensitive_errors
from .errors import UserError

MAGIC = b"CHAINLOOM-BACKUP\x00\x01"
SCRYPT_N = 262144
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
FILES = frozenset({"wallets-vault.sqlite3", "transactions.sqlite3", "settings.json"})
ACTIVE_FILE = "active-data.json"


@dataclass(frozen=True)
class BackupSummary:
    created_at: str
    app_version: str
    wallets: int
    groups: int
    transactions: int
    pending_transactions: int
    batches: int
    unfinished_batches: int
    has_settings: bool


@dataclass(frozen=True)
class VerifiedBackup:
    summary: BackupSummary
    files: dict[str, bytes] = field(repr=False)


def _check(cancelled):
    if cancelled is not None and cancelled.is_set():
        raise UserError("备份恢复操作已取消。")


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=unique)


def _database(data, required, optional=()):
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(data)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("Invalid database")
        objects = connection.execute(
            "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {name for name, kind in objects if kind == "table"}
        if any(kind in {"trigger", "view"} for _, kind in objects):
            raise ValueError("Unsupported database objects")
        if not set(required) <= tables or tables - set(required) - set(optional):
            raise ValueError("Unsupported database schema")
        for table, columns in required.items():
            actual = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if actual != set(columns.split()):
                raise ValueError("Unsupported database columns")
        return connection
    except Exception:
        connection.close()
        raise


VAULT_SCHEMA = {
    "vault_meta": "key value",
    "wallet_groups": "id name",
    "wallets": "address name group_id keystore created_at",
}
TRANSACTION_SCHEMA = {
    "transfers": "request_id chain_id sender intent tx_hash raw_tx state details created_at",
    "wallet_transfers": (
        "request_id chain_id sender network transaction_json details tx_hash state created_at"
    ),
}
BATCH_SCHEMA = {
    "batches": "id mode network asset state reason created_at",
    "batch_items": "batch_id position request_id sender recipient amount fee_cap state error",
}


def _inspect(files, created_at, app_version):
    if not {"wallets-vault.sqlite3", "transactions.sqlite3"} <= files.keys():
        raise ValueError("Missing databases")
    if files.keys() - FILES:
        raise ValueError("Unexpected files")
    with closing(_database(files["wallets-vault.sqlite3"], VAULT_SCHEMA)) as vault:
        if vault.execute("SELECT value FROM vault_meta WHERE key='version'").fetchone() != ("1",):
            raise ValueError("Unsupported vault version")
        if not vault.execute("SELECT value FROM vault_meta WHERE key='password_check'").fetchone():
            raise ValueError("Missing vault verifier")
        if vault.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Invalid wallet group")
        wallets = vault.execute("SELECT count(*) FROM wallets").fetchone()[0]
        groups = vault.execute("SELECT count(*) FROM wallet_groups").fetchone()[0]
    with closing(
        _database(files["transactions.sqlite3"], TRANSACTION_SCHEMA, BATCH_SCHEMA)
    ) as transactions:
        tables = {row[0] for row in transactions.execute("SELECT name FROM sqlite_master")}
        batches = unfinished = 0
        if {"batches", "batch_items"} & tables:
            # Validate both batch tables before the application may use them.
            with closing(
                _database(files["transactions.sqlite3"], TRANSACTION_SCHEMA | BATCH_SCHEMA)
            ):
                pass
            batches = transactions.execute("SELECT count(*) FROM batches").fetchone()[0]
            unfinished = transactions.execute(
                "SELECT count(*) FROM batches WHERE state NOT IN "
                "('completed','completed_with_errors','cancelled')"
            ).fetchone()[0]
        total = pending = 0
        for table in TRANSACTION_SCHEMA:
            total += transactions.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            pending += transactions.execute(
                f"SELECT count(*) FROM {table} WHERE state NOT IN "
                "('confirmed','failed','rejected','mismatched')"
            ).fetchone()[0]
    if "settings.json" in files:
        from .desktop.settings import Preferences

        settings = _json(files["settings.json"])
        settings.pop("project_id", None)
        if settings.get("network") == "sepolia":
            settings["network"] = "ethereum"
        for key in ("rpc_overrides", "custom_assets"):
            if isinstance(settings.get(key), dict):
                settings[key].pop("sepolia", None)
        Preferences(**settings).validate()
    return BackupSummary(
        created_at,
        app_version,
        wallets,
        groups,
        total,
        pending,
        batches,
        unfinished,
        "settings.json" in files,
    )


def _snapshot(path, cancelled):
    # SQLite's online backup includes committed WAL pages without copying live files.
    # A real temporary DB can switch out of WAL mode; an in-memory serialization
    # of a WAL source retains a WAL header and cannot be reopened on its own.
    with tempfile.TemporaryDirectory(prefix="chainloom-snapshot-") as temporary:
        snapshot = Path(temporary) / "snapshot.sqlite3"
        descriptor = os.open(snapshot, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with (
            closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(snapshot)) as destination,
        ):
            size = source.execute("PRAGMA page_count").fetchone()[0]
            size *= source.execute("PRAGMA page_size").fetchone()[0]
            if size > MAX_FILE_BYTES:
                raise UserError("单个数据文件超过 64 MiB，请先整理数据后再备份。")
            source.backup(destination, pages=256, progress=lambda *_: _check(cancelled))
            destination.execute("PRAGMA journal_mode=DELETE")
        data = snapshot.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise UserError("单个数据文件超过 64 MiB，请先整理数据后再备份。")
    return data


def _pack(files, created_at, app_version):
    manifest = {
        "format_version": 1,
        "app_version": app_version,
        "created_at": created_at,
        "files": {
            name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in files.items()
        },
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest).encode())
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


def _unpack(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if (
            len(names) != len(set(names))
            or "manifest.json" not in names
            or set(names) - FILES - {"manifest.json"}
            or any(entry.file_size > MAX_FILE_BYTES for entry in entries)
            or sum(entry.file_size for entry in entries) > MAX_TOTAL_BYTES
        ):
            raise ValueError("Invalid archive contents")
        if archive.getinfo("manifest.json").file_size > 65536:
            raise ValueError("Oversized manifest")
        manifest = _json(archive.read("manifest.json"))
        if type(manifest["format_version"]) is not int or manifest["format_version"] != 1:
            raise ValueError("Unsupported backup version")
        created = datetime.fromisoformat(manifest["created_at"])
        if created.tzinfo is None or not isinstance(manifest["app_version"], str):
            raise ValueError("Invalid backup metadata")
        if set(manifest["files"]) != set(names) - {"manifest.json"}:
            raise ValueError("Manifest does not match archive")
        files = {}
        for name, expected in manifest["files"].items():
            data = archive.read(name)
            if (
                len(data) != expected["size"]
                or hashlib.sha256(data).hexdigest() != expected["sha256"]
            ):
                raise ValueError("File checksum mismatch")
            files[name] = data
    return VerifiedBackup(_inspect(files, manifest["created_at"], manifest["app_version"]), files)


def _decrypt(data, password, cancelled):
    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 44:
        raise UserError("备份格式或版本不受支持，或文件不完整。")
    offset = len(MAGIC)
    salt, nonce = data[offset : offset + 16], data[offset + 16 : offset + 28]
    _check(cancelled)
    key = bytearray(scrypt(password.encode("utf-8"), salt, 32, N=SCRYPT_N, r=8, p=1))
    try:
        _check(cancelled)
        cipher = AES.new(bytes(key), AES.MODE_GCM, nonce=nonce)
        cipher.update(data[: offset + 28])
        try:
            return cipher.decrypt_and_verify(data[offset + 28 : -16], data[-16:])
        except ValueError:
            raise UserError("备份密码不正确，或备份文件已损坏。") from None
    finally:
        key[:] = b"\0" * len(key)


def read_backup(path: Path, password: str, cancelled: Event | None = None) -> VerifiedBackup:
    with sensitive_errors(password):
        _check(cancelled)
        if not 8 <= len(password) <= 256:
            raise UserError("备份密码请使用 8–256 个字符。")
        with path.open("rb") as stream:
            data = stream.read(MAX_TOTAL_BYTES + 1)
        if len(data) > MAX_TOTAL_BYTES:
            raise UserError("备份包超过 128 MiB，无法读取。")
        payload = _decrypt(data, password, cancelled)
        _check(cancelled)
        try:
            return _unpack(payload)
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            sqlite3.Error,
            zipfile.BadZipFile,
            RuntimeError,
            zlib.error,
        ):
            raise UserError("备份内容校验失败，文件不完整或数据版本不受支持。") from None


def _write_new(path, data):
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def create_backup(directory: Path, path: Path, password: str, cancelled=None) -> BackupSummary:
    with sensitive_errors(password):
        if not 8 <= len(password) <= 256:
            raise UserError("备份密码请使用 8–256 个字符。")
        if path.exists() or path.is_symlink():
            raise UserError("备份文件已存在，请选择新的文件名。")
        files = {}
        for name in ("wallets-vault.sqlite3", "transactions.sqlite3"):
            _check(cancelled)
            source = directory / name
            if not source.is_file():
                raise UserError("请先创建钱包库，再备份完整的本地数据。")
            files[name] = _snapshot(source, cancelled)
        settings = directory / "settings.json"
        if settings.exists():
            with settings.open("rb") as stream:
                files[settings.name] = stream.read(MAX_FILE_BYTES + 1)
        if any(len(data) > MAX_FILE_BYTES for data in files.values()):
            raise UserError("单个数据文件超过 64 MiB，无法备份。")
        if sum(map(len, files.values())) > MAX_TOTAL_BYTES - 65536:
            raise UserError("本地数据超过 128 MiB，无法备份。")
        created = datetime.now(UTC).isoformat()
        try:
            summary = _inspect(files, created, "0.1.0")
        except (ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
            raise UserError("本地数据校验失败，未生成备份。请保留原数据并检查文件。") from None
        payload = _pack(files, created, "0.1.0")
        salt, nonce = get_random_bytes(16), get_random_bytes(12)
        header = MAGIC + salt + nonce
        _check(cancelled)
        key = bytearray(scrypt(password.encode("utf-8"), salt, 32, N=SCRYPT_N, r=8, p=1))
        try:
            cipher = AES.new(bytes(key), AES.MODE_GCM, nonce=nonce)
            cipher.update(header)
            encrypted, tag = cipher.encrypt_and_digest(payload)
        finally:
            key[:] = b"\0" * len(key)
        data = header + encrypted + tag
        if len(data) > MAX_TOTAL_BYTES:
            raise UserError("备份包超过 128 MiB，无法保存。")
        _check(cancelled)
        descriptor, name = tempfile.mkstemp(prefix=".chainloom-backup-", dir=path.parent)
        temporary = Path(name)
        reserved = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Read back and authenticate the actual saved bytes before publication.
            verified = read_backup(temporary, password, cancelled)
            if verified.summary != summary:
                raise UserError("备份写入校验失败，请检查目标磁盘。")
            _check(cancelled)
            # Reserve without overwriting, then publish on filesystems without hard links too.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            reserved = True
            os.replace(temporary, path)
            reserved = False
        finally:
            temporary.unlink(missing_ok=True)
            if reserved:
                path.unlink(missing_ok=True)
        record_operation("加密备份完成", 钱包数量=summary.wallets, 交易数量=summary.transactions)
        return summary


def restore_backup(backup: VerifiedBackup, target: Path, cancelled=None) -> Path:
    """Only create a new directory. Failure leaves the active data untouched."""
    _check(cancelled)
    try:
        target.mkdir(mode=0o700)
    except FileExistsError:
        raise UserError("恢复目录已存在，请选择新的目录；原有数据不会被覆盖。") from None
    try:
        for name, data in backup.files.items():
            _check(cancelled)
            if name not in FILES:
                raise UserError("备份中存在不支持的文件。")
            _write_new(target / name, data)
            if (target / name).read_bytes() != data:
                raise UserError("恢复文件写入校验失败，请检查目标磁盘。")
        # Every unfinished batch requires a fresh preview and explicit confirmation.
        with closing(sqlite3.connect(target / "transactions.sqlite3")) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='batches'").fetchone():
                with connection:
                    connection.execute(
                        "UPDATE batches SET state='paused',reason='从备份恢复，请先核对链上状态，"
                        "再重新预览剩余任务' WHERE state NOT IN "
                        "('completed','completed_with_errors','cancelled')"
                    )
        _check(cancelled)
    except BaseException:
        shutil.rmtree(target)
        raise
    record_operation("备份恢复完成", 钱包数量=backup.summary.wallets)
    return target.resolve()


def active_directory(entry_directory: Path) -> Path:
    path = entry_directory / ACTIVE_FILE
    if not path.exists():
        return entry_directory
    try:
        data = _json(path.read_bytes())
        target = Path(data["directory"])
        if data["version"] != 1 or not target.is_absolute() or not target.is_dir():
            raise ValueError("Invalid active directory")
        if not all(
            (target / name).is_file() for name in ("wallets-vault.sqlite3", "transactions.sqlite3")
        ):
            raise ValueError("Incomplete restored directory")
        return target.resolve()
    except (ValueError, KeyError, TypeError, OSError):
        raise UserError(
            "恢复的数据目录无法访问，请连接对应磁盘，或检查 active-data.json。"
        ) from None


def select_directory(entry_directory: Path, target: Path):
    descriptor, name = tempfile.mkstemp(prefix=".active-data-", dir=entry_directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"version": 1, "directory": str(target.resolve())}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, entry_directory / ACTIVE_FILE)
    finally:
        temporary.unlink(missing_ok=True)
