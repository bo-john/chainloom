import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .diagnostics import record_operation
from .errors import UserError

STATE_NAMES = {
    "signed": "已签名",
    "submitted": "已提交",
    "unknown": "结果不明确",
    "mined": "链上确认中",
    "confirmed": "转账成功",
    "failed": "链上执行失败",
    "rejected": "已拒绝",
    "awaiting_wallet": "等待外部钱包",
    "wallet_unknown": "外部钱包结果不明确",
    "verifying": "正在核验",
    "mismatched": "交易不一致",
}


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        path = directory / "transactions.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS transfers (
            request_id TEXT PRIMARY KEY,
            chain_id INTEGER NOT NULL,
            sender TEXT NOT NULL,
            intent TEXT NOT NULL,
            tx_hash TEXT NOT NULL UNIQUE,
            raw_tx TEXT NOT NULL,
            state TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS wallet_transfers (
            request_id TEXT PRIMARY KEY,
            chain_id INTEGER NOT NULL,
            sender TEXT NOT NULL,
            network TEXT NOT NULL,
            transaction_json TEXT NOT NULL,
            details TEXT NOT NULL,
            tx_hash TEXT UNIQUE,
            state TEXT NOT NULL DEFAULT 'awaiting_wallet',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")
        self.db.commit()

    def close(self):
        self.db.close()

    @contextmanager
    def lock(self):
        # Native advisory locks release automatically after a crash.
        if os.name == "nt":
            import msvcrt
        else:
            import fcntl
        fd = os.open(self.directory / "transfer.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise UserError("Another transfer command is running") from None
            else:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise UserError("Another transfer command is running") from None
            yield
        finally:
            os.close(fd)

    def get(self, request_id: str):
        row = self.db.execute(
            "SELECT * FROM transfers WHERE request_id=?", (request_id,)
        ).fetchone()
        return self.decode(row) if row else None

    @staticmethod
    def decode(row):
        result = dict(row)
        result["details"] = json.loads(result["details"])
        return result

    def active(self, chain_id: int, sender: str):
        local = self.db.execute(
            """SELECT request_id FROM transfers
            WHERE chain_id=? AND sender=? AND state IN ('signed', 'submitted', 'unknown', 'mined')""",
            (chain_id, sender),
        ).fetchone()
        return (
            local
            or self.db.execute(
                """SELECT request_id FROM wallet_transfers WHERE chain_id=? AND sender=?
            AND state NOT IN ('confirmed', 'failed', 'rejected')""",
                (chain_id, sender),
            ).fetchone()
        )

    def wallet_save(self, request_id, network, sender, transaction, details):
        with self.db:
            self.db.execute(
                """INSERT INTO wallet_transfers
                (request_id,chain_id,sender,network,transaction_json,details)
                VALUES (?,?,?,?,?,?)""",
                (
                    request_id,
                    transaction["chainId"],
                    sender,
                    network,
                    json.dumps(transaction),
                    json.dumps(details),
                ),
            )
        record_operation(
            "保存外部钱包转账记录",
            请求编号=request_id,
            网络=network,
            发送地址=sender,
            收款地址=details.get("to"),
            金额=details.get("amount"),
        )

    def wallet_get(self, request_id):
        row = self.db.execute(
            "SELECT * FROM wallet_transfers WHERE request_id=?", (request_id,)
        ).fetchone()
        if not row:
            return None
        result = self.decode(row)
        result["transaction"] = json.loads(result.pop("transaction_json"))
        return result

    def wallet_update(self, request_id, state, tx_hash=None):
        previous = self.wallet_get(request_id)
        with self.db:
            self.db.execute(
                """UPDATE wallet_transfers SET state=?,
                tx_hash=COALESCE(?, tx_hash) WHERE request_id=?""",
                (state, tx_hash, request_id),
            )
        if previous and (
            previous["state"] != state or (tx_hash and tx_hash != previous["tx_hash"])
        ):
            record_operation(
                "更新外部钱包转账状态",
                请求编号=request_id,
                原状态=STATE_NAMES.get(previous["state"], previous["state"]),
                新状态=STATE_NAMES.get(state, state),
                交易哈希=tx_hash or previous["tx_hash"],
            )

    def wallet_history(self, limit=100):
        return [
            self.wallet_get(row[0])
            for row in self.db.execute(
                "SELECT request_id FROM wallet_transfers ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        ]

    def save(self, request_id, chain_id, sender, intent, tx_hash, raw_tx, details):
        with self.db:
            self.db.execute(
                """INSERT INTO transfers
                (request_id,chain_id,sender,intent,tx_hash,raw_tx,state,details)
                VALUES (?,?,?,?,?,?,'signed',?)""",
                (request_id, chain_id, sender, intent, tx_hash, raw_tx, json.dumps(details)),
            )
        record_operation(
            "保存本地签名转账记录",
            请求编号=request_id,
            链编号=chain_id,
            发送地址=sender,
            收款地址=details.get("to"),
            金额=details.get("amount"),
            资产=details.get("symbol", details.get("asset")),
            交易哈希=tx_hash,
            执行手续费上限=details.get("max_execution_fee_wei"),
            状态="已签名",
        )

    def update(self, tx_hash: str, state: str):
        previous = self.db.execute(
            "SELECT request_id,state FROM transfers WHERE tx_hash=?", (tx_hash,)
        ).fetchone()
        with self.db:
            self.db.execute("UPDATE transfers SET state=? WHERE tx_hash=?", (state, tx_hash))
        if previous and previous["state"] != state:
            record_operation(
                "更新本地转账状态",
                请求编号=previous["request_id"],
                交易哈希=tx_hash,
                原状态=STATE_NAMES.get(previous["state"], previous["state"]),
                新状态=STATE_NAMES.get(state, state),
            )

    def history(self, limit: int = 20):
        return [
            self.decode(row)
            for row in self.db.execute(
                """SELECT request_id,chain_id,sender,
            tx_hash,state,details,created_at FROM transfers ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            )
        ]
