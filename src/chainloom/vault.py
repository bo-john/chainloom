"""Local wallet inventory and signing; decrypted private keys stay in this module.

Wallet names, addresses and groups are ordinary SQLite metadata. Each private key
is protected by the standard Ethereum V3 scrypt keystore format, not custom crypto.
"""

import csv
import json
import os
import re
import secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from threading import Event, RLock

from eth_account import Account

from .diagnostics import record_operation, sensitive_errors
from .errors import UserError

SCRYPT_N = 262144
MAX_IMPORT_ROWS = 1000
MAX_IMPORT_BYTES = 1024 * 1024


class VaultLocked(UserError):
    def __init__(self):
        super().__init__("钱包库已锁定，操作已停止，请重新解锁。")


class ImportCancelled(UserError):
    def __init__(self):
        super().__init__("导入已取消，本次内容未写入钱包库。")


@dataclass(frozen=True)
class ImportRow:
    private_key: str = field(repr=False)
    name: str = ""
    address: str = ""
    line: int = 0


@dataclass(frozen=True)
class ImportReport:
    added: int
    duplicates: int
    invalid: tuple[tuple[int, str], ...]


def clean_label(value: str, title: str, limit: int) -> str:
    value = value.strip()
    if len(value) > limit or any(ord(char) < 32 for char in value):
        raise UserError(f"{title}请控制在 {limit} 个字符以内，且不要包含控制字符。")
    return value


def parse_import(content: str) -> list[ImportRow]:
    if len(content.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise UserError("导入内容不能超过 1 MB，请分批导入。")
    lines = [
        (index, value.strip())
        for index, value in enumerate(content.lstrip("\ufeff").splitlines(), 1)
        if value.strip()
    ]
    if not lines:
        raise UserError("请先粘贴私钥，或选择 TXT / CSV 文件。")
    aliases = {
        "private_key": "private_key",
        "私钥": "private_key",
        "name": "name",
        "名称": "name",
        "address": "address",
        "地址": "address",
    }

    def columns(value, line):
        try:
            return next(csv.reader([value], strict=True))
        except csv.Error:
            raise UserError(f"第 {line} 行的 CSV 格式不正确，请检查引号及分隔符。") from None

    header = columns(lines[0][1], lines[0][0])
    if any(item.strip().lower() in {"group", "分组"} for item in header):
        raise UserError("CSV 不再支持 group / 分组列，请删除该列，并在导入窗口选择统一分组。")
    mapped = [aliases.get(item.strip().lower()) for item in header]
    if "private_key" in mapped:
        if None in mapped or len(set(mapped)) != len(mapped):
            raise UserError("CSV 表头请使用 private_key、name、address，或对应的中文字段。")
        rows = []
        # One wallet per physical line: reject multiline/ambiguous records.
        for number, line in lines[1:]:
            values = columns(line, number)
            if len(values) != len(mapped):
                raise UserError(f"第 {number} 行的列数与表头不一致，请检查 CSV 格式。")
            item = dict(zip(mapped, values, strict=True))
            rows.append(ImportRow(**item, line=number))
    else:
        if any("," in line or "\t" in line for _, line in lines):
            raise UserError("多列内容需要 CSV 表头，例如：private_key,name,address。")
        rows = [ImportRow(private_key=value, line=number) for number, value in lines]
    if not rows or len(rows) > MAX_IMPORT_ROWS:
        raise UserError(f"每批请导入 1–{MAX_IMPORT_ROWS} 个钱包。")
    return rows


def _private_operation(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        supplied = kwargs.get("password")
        if supplied is None and function.__name__ in {"create", "unlock"} and args:
            supplied = args[0]
        with self._mutex:
            current = bytes(self._password) if self._password is not None else None
        changing = function.__name__ == "change_password"
        secrets = (
            (kwargs.get("current_password", args[0] if args else None),
             kwargs.get("new_password", args[1] if len(args) > 1 else None))
            if changing else ()
        )
        with sensitive_errors(supplied, current, *secrets):
            return function(self, *args, **kwargs)

    return wrapped


class WalletVault:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "wallets-vault.sqlite3"
        self._mutex = RLock()
        self._generation = 0
        self._password = None

    @property
    def exists(self):
        return self.path.exists()

    @property
    def unlocked(self):
        with self._mutex:
            return self._password is not None

    def lock(self):
        with self._mutex:
            self._generation += 1
            if self._password is not None:
                self._password[:] = b"\0" * len(self._password)
                self._password = None
                record_operation("钱包库已锁定")

    @property
    def session_generation(self):
        with self._mutex:
            self._check()
            return self._generation

    @_private_operation
    def sign_transaction(self, sender, transaction, generation, cancelled=None):
        """Decrypt only the selected wallet; locking invalidates in-flight decryption."""
        with self._mutex, self._connection() as connection:
            self._check(generation, cancelled)
            row = connection.execute(
                "SELECT keystore FROM wallets WHERE address=?", (sender,)
            ).fetchone()
            if row is None:
                raise UserError("所选钱包不在本地钱包库中，请重新选择。")
            password = bytearray(self._password)
        key = None
        account = None
        try:
            try:
                key = bytearray(Account.decrypt(json.loads(row[0]), bytes(password)))
                account = Account.from_key(key)
            except (ValueError, TypeError, KeyError):
                raise UserError("所选钱包无法解密，请检查钱包库文件及密码。") from None
            with self._mutex:
                self._check(generation, cancelled)
                if (
                    account.address.lower() != sender.lower()
                    or transaction.get("from", "").lower() != sender.lower()
                ):
                    raise UserError("签名钱包与发送地址不一致，已停止发送。")
                return account.sign_transaction(transaction)
        finally:
            password[:] = b"\0" * len(password)
            if key is not None:
                key[:] = b"\0" * len(key)
            account = None

    def _check(self, generation=None, cancelled: Event | None = None):
        if cancelled is not None and cancelled.is_set():
            raise ImportCancelled()
        if self._password is None or (generation is not None and generation != self._generation):
            raise VaultLocked()

    @contextmanager
    def _connection(self):
        if not self.exists:
            raise UserError("请先创建钱包库。")
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            version = connection.execute(
                "SELECT value FROM vault_meta WHERE key='version'"
            ).fetchone()
            if not version or version[0] != "1":
                raise UserError("钱包库版本不受支持，请保留原文件并联系维护者。")
            yield connection
        except sqlite3.DatabaseError:
            raise UserError("钱包库无法读取，请保留原文件并检查磁盘或备份。") from None
        finally:
            connection.close()

    @_private_operation
    def create(self, password: str, cancelled: Event | None = None):
        if cancelled is not None and cancelled.is_set():
            raise ImportCancelled()
        if not 8 <= len(password) <= 256:
            raise UserError("钱包库密码请使用 8–256 个字符。请妥善保存，遗忘后无法恢复。")
        with self._mutex:
            if self.exists:
                raise UserError("钱包库已存在，不能覆盖，请使用解锁功能。")
            generation = self._generation
        # A random sentinel verifies the password without decrypting real wallets.
        verifier = Account.encrypt(
            secrets.token_bytes(32), password, kdf="scrypt", iterations=SCRYPT_N
        )
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._mutex:
            if generation != self._generation or (cancelled and cancelled.is_set()):
                raise ImportCancelled()
            fd, temporary = tempfile.mkstemp(
                dir=self.directory, prefix=".wallet-vault-", suffix=".tmp"
            )
            os.close(fd)
            try:
                connection = sqlite3.connect(temporary)
                try:
                    with connection:
                        connection.executescript("""
                            CREATE TABLE vault_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                            CREATE TABLE wallet_groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
                            CREATE TABLE wallets (
                                address TEXT PRIMARY KEY COLLATE NOCASE,
                                name TEXT NOT NULL,
                                group_id INTEGER NOT NULL REFERENCES wallet_groups(id),
                                keystore TEXT NOT NULL,
                                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                            );
                        """)
                        connection.executemany(
                            "INSERT INTO vault_meta VALUES (?,?)",
                            [("version", "1"), ("password_check", json.dumps(verifier))],
                        )
                        connection.execute(
                            "INSERT INTO wallet_groups (id,name) VALUES (1,'未分组')"
                        )
                finally:
                    connection.close()
                # Atomic publication, never replacing an existing vault.
                os.link(temporary, self.path)
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except FileExistsError:
                raise UserError("钱包库已存在，不能覆盖，请使用解锁功能。") from None
            finally:
                os.unlink(temporary)
            self._password = bytearray(password.encode("utf-8"))
            self._generation += 1
            record_operation("创建钱包库成功")

    @_private_operation
    def unlock(self, password: str, cancelled: Event | None = None):
        if cancelled is not None and cancelled.is_set():
            raise ImportCancelled()
        with self._mutex:
            generation = self._generation
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM vault_meta WHERE key='password_check'"
            ).fetchone()
        try:
            verifier = json.loads(row[0])
            params = verifier["crypto"]["kdfparams"]
            if (
                verifier["crypto"]["kdf"] != "scrypt"
                or params["n"] != SCRYPT_N
                or params["r"] != 8
                or params["p"] != 1
                or params["dklen"] != 32
            ):
                raise ValueError("Unsupported KDF parameters")
            Account.decrypt(verifier, password)
        except (ValueError, TypeError, KeyError, IndexError):
            raise UserError("密码不正确，或钱包库文件已损坏。") from None
        with self._mutex:
            if generation != self._generation or (cancelled and cancelled.is_set()):
                raise ImportCancelled()
            self.lock()
            self._password = bytearray(password.encode("utf-8"))
            record_operation("解锁钱包库成功")

    @_private_operation
    def change_password(self, current_password: str, new_password: str, cancelled=None,
                        progress=None):
        """Re-encrypt all keys, then atomically replace ciphertext and verifier."""
        def check_rotation(generation=None):
            if cancelled is not None and cancelled.is_set():
                raise ImportCancelled()
            if generation is not None and generation != self._generation:
                raise VaultLocked()

        if not 8 <= len(new_password) <= 256:
            raise UserError("钱包库密码请使用 8–256 个字符。")
        if current_password == new_password:
            raise UserError("新密码不能与当前密码相同。")
        with self._mutex, self._connection() as connection:
            check_rotation()
            generation = self._generation
            was_unlocked = self._password is not None
            verifier = connection.execute(
                "SELECT value FROM vault_meta WHERE key='password_check'"
            ).fetchone()[0]
            original = [tuple(row) for row in connection.execute(
                "SELECT address,keystore FROM wallets ORDER BY address"
            )]
        try:
            key = bytearray(Account.decrypt(json.loads(verifier), current_password))
        except (ValueError, TypeError, KeyError):
            raise UserError("当前密码不正确，或钱包库文件已损坏。") from None
        key[:] = b"\0" * len(key)
        encrypted = []
        for index, (address, keystore) in enumerate(original, 1):
            with self._mutex:
                check_rotation(generation)
            key = None
            try:
                key = bytearray(Account.decrypt(json.loads(keystore), current_password))
                if Account.from_key(key).address.lower() != address.lower():
                    raise ValueError("Address mismatch")
                replacement = Account.encrypt(
                    bytes(key), new_password, kdf="scrypt", iterations=SCRYPT_N
                )
                encrypted.append((json.dumps(replacement), address))
            except (ValueError, TypeError, KeyError):
                raise UserError("钱包无法重新加密，本次未修改密码，请检查钱包库文件。") from None
            finally:
                if key is not None:
                    key[:] = b"\0" * len(key)
            if progress:
                progress(index, len(original))
        with self._mutex:
            check_rotation(generation)
        replacement_verifier = json.dumps(Account.encrypt(
            secrets.token_bytes(32), new_password, kdf="scrypt", iterations=SCRYPT_N
        ))
        with self._mutex, self._connection() as connection, connection:
            check_rotation(generation)
            connection.execute("BEGIN IMMEDIATE")
            # Importing or another process changing the password must not leave
            # any wallet encrypted with a different password.
            latest = [tuple(row) for row in connection.execute(
                "SELECT address,keystore FROM wallets ORDER BY address"
            )]
            latest_verifier = connection.execute(
                "SELECT value FROM vault_meta WHERE key='password_check'"
            ).fetchone()[0]
            if latest != original or latest_verifier != verifier:
                raise UserError("钱包库内容已变更，本次未修改密码，请重试。")
            connection.executemany("UPDATE wallets SET keystore=? WHERE address=?", encrypted)
            connection.execute(
                "UPDATE vault_meta SET value=? WHERE key='password_check'",
                (replacement_verifier,),
            )
            connection.commit()
            if self._password is not None:
                self._password[:] = b"\0" * len(self._password)
            self._password = bytearray(new_password.encode("utf-8")) if was_unlocked else None
            self._generation += 1
        record_operation("修改钱包库密码成功")

    def wallets(self, search="", group_id=None):
        with self._mutex, self._connection() as connection:
            self._check()
            rows = connection.execute("""SELECT w.address,w.name,w.group_id,g.name AS group_name,w.created_at
                FROM wallets w JOIN wallet_groups g ON w.group_id=g.id ORDER BY w.created_at,w.rowid""").fetchall()
            search = search.strip().casefold()
            return [
                dict(row)
                for row in rows
                if (group_id is None or row["group_id"] == group_id)
                and (
                    not search
                    or any(
                        search in row[key].casefold() for key in ("name", "address", "group_name")
                    )
                )
            ]

    def groups(self):
        with self._mutex, self._connection() as connection:
            self._check()
            return [
                dict(row)
                for row in connection.execute("SELECT id,name FROM wallet_groups ORDER BY id")
            ]

    @_private_operation
    def create_group(self, name):
        name = clean_label(name, "分组名称", 60)
        if not name:
            raise UserError("请输入分组名称。")
        with self._mutex, self._connection() as connection, connection:
            self._check()
            connection.execute("INSERT OR IGNORE INTO wallet_groups(name) VALUES (?)", (name,))
            result = connection.execute(
                "SELECT id FROM wallet_groups WHERE name=?", (name,)
            ).fetchone()[0]
        record_operation("保存钱包分组", 分组编号=result, 分组名称=name)
        return result

    @_private_operation
    def update_wallets(self, addresses, *, group_id=None, name=None):
        if not addresses:
            raise UserError("请先选择钱包。")
        if name is not None:
            name = clean_label(name, "钱包名称", 80)
            if not name or len(addresses) != 1:
                raise UserError("重命名时请选择一个钱包，并填写名称。")
        with self._mutex, self._connection() as connection, connection:
            self._check()
            placeholders = ",".join("?" for _ in addresses)
            before = [
                {"地址": row["address"], "名称": row["name"], "分组编号": row["group_id"]}
                for row in connection.execute(
                    f"SELECT address,name,group_id FROM wallets WHERE address IN ({placeholders})",
                    addresses,
                )
            ]
            if group_id is not None:
                if not connection.execute(
                    "SELECT 1 FROM wallet_groups WHERE id=?", (group_id,)
                ).fetchone():
                    raise UserError("所选分组不存在，请重新选择。")
                connection.executemany(
                    "UPDATE wallets SET group_id=? WHERE address=?",
                    [(group_id, item) for item in addresses],
                )
            if name is not None:
                connection.execute(
                    "UPDATE wallets SET name=? WHERE address=?", (name, addresses[0])
                )

        record_operation(
            "修改钱包信息成功", 修改前=before, 钱包地址=addresses, 新名称=name, 新分组编号=group_id
        )

    @_private_operation
    def import_wallets(self, rows: list[ImportRow], group_id=None, cancelled=None, progress=None):
        if not 1 <= len(rows) <= MAX_IMPORT_ROWS:
            raise UserError(f"每批请导入 1–{MAX_IMPORT_ROWS} 个钱包。")
        with self._mutex, self._connection() as connection:
            self._check(cancelled=cancelled)
            if group_id is None:
                selected = connection.execute(
                    "SELECT id,name FROM wallet_groups WHERE name='未分组'"
                ).fetchone()
            else:
                if type(group_id) is not int:
                    raise UserError("请选择一个有效的导入分组。")
                selected = connection.execute(
                    "SELECT id,name FROM wallet_groups WHERE id=?", (group_id,)
                ).fetchone()
            if selected is None:
                raise UserError("所选分组不存在，请重新选择。")
            group_id, group_name = selected["id"], selected["name"]
            generation = self._generation
            password = bytearray(self._password)
            seen = {row[0].lower() for row in connection.execute("SELECT address FROM wallets")}
        record_operation("开始批量导入钱包", 总行数=len(rows), 导入分组=group_name)
        encrypted, invalid, duplicates = [], [], 0
        try:
            for index, row in enumerate(rows, 1):
                with self._mutex:
                    self._check(generation, cancelled)
                try:
                    raw = row.private_key.strip().removeprefix("0x").removeprefix("0X")
                    if not re.fullmatch(r"[0-9a-fA-F]{64}", raw):
                        raise UserError("私钥格式不正确")
                    try:
                        account = Account.from_key(bytes.fromhex(raw))
                    except (ValueError, TypeError, OverflowError):
                        raise UserError("私钥超出有效范围") from None
                    if (
                        row.address.strip()
                        and row.address.strip().lower() != account.address.lower()
                    ):
                        raise UserError("地址与私钥不匹配")
                    name = (
                        clean_label(row.name, "钱包名称", 80)
                        or f"钱包 {account.address[:8]}…{account.address[-4:]}"
                    )
                    if account.address.lower() in seen:
                        duplicates += 1
                    else:
                        keystore = Account.encrypt(
                            account.key, bytes(password), kdf="scrypt", iterations=SCRYPT_N
                        )
                        encrypted.append((account.address, name, json.dumps(keystore)))
                        seen.add(account.address.lower())
                except UserError as error:
                    invalid.append((row.line or index, str(error)))
                if progress:
                    progress(index, len(rows))
            added = 0
            added_wallets = []
            with self._mutex, self._connection() as connection, connection:
                self._check(generation, cancelled)
                if not connection.execute(
                    "SELECT 1 FROM wallet_groups WHERE id=?", (group_id,)
                ).fetchone():
                    raise UserError("所选分组不存在，本次内容未写入，请重新选择。")
                for wallet_address, name, keystore in encrypted:
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO wallets(address,name,group_id,keystore) VALUES (?,?,?,?)",
                        (wallet_address, name, group_id, keystore),
                    )
                    added += cursor.rowcount
                    if cursor.rowcount:
                        added_wallets.append(
                            {"地址": wallet_address, "名称": name, "分组": group_name}
                        )
                    if not cursor.rowcount:
                        duplicates += 1
            record_operation(
                "批量导入钱包完成",
                总行数=len(rows),
                新增数量=added,
                重复数量=duplicates,
                无效行=invalid,
                新增钱包=added_wallets,
            )
            return ImportReport(added, duplicates, tuple(invalid))
        finally:
            password[:] = b"\0" * len(password)
