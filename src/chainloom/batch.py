"""Batch manifests and durable task state; private keys never enter a manifest."""

import csv
import io
import json
import sqlite3
import uuid
from contextlib import contextmanager

from .errors import UserError
from .storage import Store

MAX_BATCH_ROWS = 100
MAX_BATCH_BYTES = 1_000_000
MODES = {
    "distribute": "一对多 · 批量分发",
    "collect": "多对一 · 批量归集",
    "mapping": "多对多 · 按清单转账",
}
BATCH_STATES = {
    "running": "执行中",
    "paused": "已暂停",
    "cancelled": "已结束",
    "completed": "全部成功",
    "completed_with_errors": "已完成（含失败）",
}
ITEM_STATES = {
    "pending": "待发送",
    "signed": "已签名，待广播",
    "submitted": "已提交",
    "mined": "链上确认中",
    "unknown": "结果待确认",
    "confirmed": "已成功",
    "failed": "链上失败",
    "skipped": "未发送，已取消",
}


def parse_manifest(content, mode="mapping", sender="", recipient=""):
    if mode not in MODES:
        raise UserError("不支持的批量模式。")
    if len(content.encode("utf-8")) > MAX_BATCH_BYTES:
        raise UserError("清单最多 1 MB。")
    content = content.lstrip("\ufeff")
    try:
        records = [
            row
            for row in csv.reader(io.StringIO(content), strict=True)
            if any(cell.strip() for cell in row)
        ]
    except csv.Error:
        raise UserError("清单格式不正确，请使用 CSV。") from None
    if not records:
        raise UserError("请先填写转账清单。")
    aliases = {
        "sender": "sender",
        "from": "sender",
        "发送钱包": "sender",
        "发送地址": "sender",
        "recipient": "recipient",
        "to": "recipient",
        "收款地址": "recipient",
        "amount": "amount",
        "数量": "amount",
        "金额": "amount",  # Compatibility with previously exported/imported Chinese headers.
    }
    headers = [aliases.get(cell.strip().lower()) for cell in records[0]]
    if any(headers):
        if None in headers or len(set(headers)) != len(headers):
            raise UserError("表头仅支持 sender、recipient、amount；不要导入私钥。")
        records = records[1:]
    else:
        headers = ["sender", "recipient", "amount"]
        if len(records[0]) == 2 and mode != "mapping":
            headers = ["recipient", "amount"] if mode == "distribute" else ["sender", "amount"]
    result = []
    for index, row in enumerate(records, 1):
        if len(row) != len(headers):
            raise UserError(f"第 {index} 行列数不正确。")
        item = dict(zip(headers, (cell.strip() for cell in row)))
        item.setdefault("sender", sender)
        item.setdefault("recipient", recipient)
        item.setdefault("amount", "")
        if mode == "distribute":
            if sender and item["sender"] and item["sender"].lower() != sender.lower():
                raise UserError(f"第 {index} 行发送钱包与一对多设置不一致。")
            item["sender"] = item["sender"] or sender
        if mode == "collect":
            if recipient and item["recipient"] and item["recipient"].lower() != recipient.lower():
                raise UserError(f"第 {index} 行收款地址与多对一设置不一致。")
            item["recipient"] = item["recipient"] or recipient
        result.append(item)
    if not 1 <= len(result) <= MAX_BATCH_ROWS:
        raise UserError(f"每批需要 1–{MAX_BATCH_ROWS} 笔转账。")
    return result


class BatchStore:
    def __init__(self, directory):
        self.store = Store(directory)
        self.db = self.store.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY, mode TEXT NOT NULL, network TEXT NOT NULL,
            asset TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS batch_items (
            batch_id TEXT NOT NULL, position INTEGER NOT NULL, request_id TEXT NOT NULL UNIQUE,
            sender TEXT NOT NULL, recipient TEXT NOT NULL, amount TEXT NOT NULL,
            fee_cap TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(batch_id, position)
        )""")
        self.db.commit()

    def close(self):
        self.store.close()

    def create(self, preview):
        batch_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO batches(id,mode,network,asset,state) VALUES (?,?,?,?,'paused')",
                (batch_id, preview["mode"], preview["network"], json.dumps(preview["asset"])),
            )
            for index, item in enumerate(preview["items"], 1):
                self.db.execute(
                    """INSERT INTO batch_items(batch_id,position,request_id,sender,recipient,amount,fee_cap)
                    VALUES (?,?,?,?,?,?,?)""",
                    (
                        batch_id,
                        index,
                        f"batch-{batch_id}-{index}",
                        item["sender"],
                        item["recipient"],
                        item["amount"],
                        str(item["fee_cap"]),
                    ),
                )
        return batch_id

    def get(self, batch_id):
        self.db.row_factory = sqlite3.Row
        row = self.db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise UserError("未找到批次。")
        result = dict(row)
        result["asset"] = json.loads(result["asset"])
        result["items"] = []
        for row in self.db.execute(
            "SELECT * FROM batch_items WHERE batch_id=? ORDER BY position", (batch_id,)
        ):
            item = dict(row)
            transaction = self.store.get(item["request_id"])
            item["tx_hash"] = transaction["tx_hash"] if transaction else ""
            if transaction:
                item["state"] = transaction["state"]
            result["items"].append(item)
        return result

    def history(self):
        # The picker needs summaries, not every signed transaction in every past batch.
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id,mode,network,state,reason,created_at FROM batches ORDER BY created_at DESC,rowid DESC"
            )
        ]

    def state(self, batch_id, state, reason="", only_running=False):
        with self.db:
            self.db.execute(
                "UPDATE batches SET state=?,reason=? WHERE id=?"
                + (" AND state='running'" if only_running else ""),
                (state, reason, batch_id),
            )

    def error(self, request_id, message):
        with self.db:
            self.db.execute(
                "UPDATE batch_items SET error=? WHERE request_id=?", (message, request_id)
            )

    def cancel(self, batch_id):
        with self.db:
            self.db.execute(
                "UPDATE batches SET state='cancelled',reason='用户结束批次' WHERE id=?", (batch_id,)
            )
            self.db.execute(
                """UPDATE batch_items SET state='skipped' WHERE batch_id=? AND request_id NOT IN
                (SELECT request_id FROM transfers)""",
                (batch_id,),
            )


@contextmanager
def batch_store(directory):
    store = BatchStore(directory)
    try:
        yield store
    finally:
        store.close()
