"""Local address checks shared by the batch editor and approval preview."""

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from ..errors import UserError
from ..transfer import address


def review_addresses(items, mode, wallets=(), *, warn_external=False):
    local = {
        (wallet["address"] if isinstance(wallet, dict) else wallet).lower(): wallet.get("name", "")
        if isinstance(wallet, dict)
        else ""
        for wallet in wallets
    }
    rows = [{"errors": [], "warnings": [], "info": []} for _ in items]
    recipients, payments = defaultdict(list), defaultdict(list)
    for index, entry in enumerate(items):
        normalized = {}
        for key, label in (("sender", "发送"), ("recipient", "收款")):
            value = entry[key].strip()
            if not value:
                continue  # Empty fields are still being filled in; preview validates them.
            try:
                normalized[key] = address(value).lower()
            except UserError:
                rows[index]["errors"].append(f"{label}地址格式错误（请检查校验和及零地址）")
        recipient = normalized.get("recipient")
        if recipient:
            recipients[recipient].append(index)
            if recipient in local:
                name = local[recipient] or "未命名"
                rows[index]["info"].append(f"本地收款钱包：{name}")
            elif warn_external:
                rows[index]["warnings"].append("非本地收款地址，请确认可控制该地址或为预期收款方")
        sender = normalized.get("sender")
        if sender and recipient:
            if sender == recipient:
                rows[index]["warnings"].append("自转账，请核对")
            try:
                amount = Decimal(entry["amount"].strip())
                if amount.is_finite() and amount > 0:
                    payments[(sender, recipient, amount)].append(index)
            except InvalidOperation:
                pass  # Amount validation belongs to the transfer preview.
    if mode != "collect":
        for indexes in recipients.values():
            if len(indexes) > 1:
                for index in indexes:
                    rows[index]["warnings"].append(f"重复收款地址，共 {len(indexes)} 笔")
    for indexes in payments.values():
        if len(indexes) > 1:
            positions = "、".join(str(index + 1) for index in indexes)
            for index in indexes:
                rows[index]["warnings"].append(f"疑似重复付款：第 {positions} 行，仍将分别付款")
    return rows


def review_warnings(rows):
    return [
        f"第 {index} 行：{message}"
        for index, row in enumerate(rows, 1)
        for message in row["warnings"]
    ]
