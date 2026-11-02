"""Sequential batch execution with persistent identities and bounded fee approval."""

from dataclasses import asdict
from threading import Event, Lock

from web3 import Web3
from web3.exceptions import TransactionNotFound

from ..batch import MAX_BATCH_ROWS, MODES, batch_store
from ..diagnostics import record_exception, record_operation
from ..errors import UserError
from ..transfer import ERC20_ABI, TransferRequest, address, amount_units
from .rpc import OperationCancelled, check_cancelled
from .service import VaultSigner, format_units, friendly_error
from .settings import NETWORKS, Asset


class StopSignal:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events)


class BatchSigner:
    def __init__(self, signer, store, batch_id, cancelled):
        self.signer, self.address = signer, signer.address
        self.store, self.batch_id, self.cancelled = store, batch_id, cancelled

    def sign_transaction(self, transaction):
        check_cancelled(self.cancelled)
        if self.store.get(self.batch_id)["state"] != "running":
            raise OperationCancelled()
        signed = self.signer.sign_transaction(transaction)
        check_cancelled(self.cancelled)
        return signed


class BatchService:
    def __init__(self, backend):
        self.backend = backend
        self.directory = backend.directory
        self.stops = {}
        self.execution_lock = Lock()

    def recover(self):
        # A restart never resumes authority to sign automatically.
        with batch_store(self.directory) as store, store.db:
            store.db.execute(
                "UPDATE batches SET state='paused',reason='应用已重启，请核对结果并重新确认剩余任务' WHERE state='running'"
            )

    def history(self):
        with batch_store(self.directory) as store:
            return store.history()

    def get(self, batch_id):
        with batch_store(self.directory) as store:
            return store.get(batch_id)

    def preview(self, mode, network, asset, items, wallets):
        if mode not in MODES or network not in NETWORKS:
            raise UserError("批量模式或网络不受支持。")
        if not 1 <= len(items) <= MAX_BATCH_ROWS:
            raise UserError(f"每批需要 1–{MAX_BATCH_ROWS} 笔转账。")
        allowed = {address(wallet).lower() for wallet in wallets}
        result = {
            "mode": mode,
            "network": network,
            "asset": asdict(asset),
            "items": [],
            "errors": {},
            "warnings": [],
            "senders": [],
            "total": "0",
            "fee_total": "0",
        }
        totals, seen = {}, {}
        with self.backend.service(network) as service:
            decimals, _ = service._asset(TransferRequest("", "", "1", asset.contract))
            for index, raw in enumerate(items, 1):
                check_cancelled(self.backend.cancelled)
                try:
                    request = TransferRequest(
                        raw["sender"].strip(),
                        raw["recipient"].strip(),
                        raw["amount"].strip(),
                        asset.contract,
                    ).normalized()
                    if request.sender.lower() not in allowed:
                        raise UserError("发送钱包不在已解锁的本地钱包库中。")
                    if service.store.active(service.network.chain_id, request.sender):
                        raise UserError("此钱包有待确认交易，请先到交易记录中处理。")
                    prepared = service.prepare(request)
                    units = amount_units(request.amount, decimals)
                    fee = int(prepared.preview["max_execution_fee_wei"])
                    result["items"].append(
                        {
                            "sender": request.sender,
                            "recipient": request.recipient,
                            "amount": request.amount,
                            "fee_cap": str(fee),
                            "position": index,
                        }
                    )
                    group = totals.setdefault(
                        request.sender, {"amount": 0, "fee": 0, "positions": []}
                    )
                    group["amount"] += units
                    group["fee"] += fee
                    group["positions"].append(index)
                    key = request.intent(service.network.chain_id)
                    if key in seen:
                        result["warnings"].append(
                            f"第 {index} 行与第 {seen[key]} 行完全重复，将分别付款。"
                        )
                    seen[key] = index
                    if request.sender == request.recipient:
                        result["warnings"].append(f"第 {index} 行为向自己转账。")
                except OperationCancelled:
                    raise
                except Exception as error:  # noqa: BLE001 - report each invalid row without RPC data
                    result["errors"][index] = friendly_error(error)
            if mode == "distribute" and len(totals) > 1:
                result["errors"][0] = "一对多模式必须使用同一个发送钱包。"
            if mode == "collect" and len({item["recipient"] for item in result["items"]}) > 1:
                result["errors"][0] = "多对一模式必须使用同一个收款地址。"
            for sender, group in totals.items():
                try:
                    native = service.web3.eth.get_balance(sender, "pending")
                    if asset.contract:
                        token = service.web3.eth.contract(address=asset.contract, abi=ERC20_ABI)
                        available = token.functions.balanceOf(sender).call(
                            block_identifier="pending"
                        )
                    else:
                        available = native
                    if available < group["amount"]:
                        raise UserError("该钱包累计转账数量超过余额。")
                    if native < group["fee"] + (0 if asset.contract else group["amount"]):
                        raise UserError("该钱包余额不足以完成累计转账并支付全部执行手续费。")
                except OperationCancelled:
                    raise
                except Exception as error:  # noqa: BLE001 - preserve per-sender diagnostics
                    for index in group["positions"]:
                        result["errors"][index] = friendly_error(error)
                result["senders"].append(
                    {
                        "sender": sender,
                        "amount": format_units(group["amount"], decimals),
                        "fee": format_units(group["fee"]),
                    }
                )
        result["total"] = format_units(sum(group["amount"] for group in totals.values()), decimals)
        result["fee_total"] = format_units(sum(group["fee"] for group in totals.values()))
        return result

    def create(self, preview):
        if preview["errors"] or not preview["items"]:
            raise UserError("请修正全部错误后重新预览。")
        with batch_store(self.directory) as store:
            batch_id = store.create(preview)
        record_operation("创建批量转账任务", 批次编号=batch_id, 笔数=len(preview["items"]))
        return batch_id

    def preview_remaining(self, batch_id, wallets):
        batch = self.get(batch_id)
        if batch["state"] not in {"paused"}:
            raise UserError("请先暂停批次。")
        remaining = [item for item in batch["items"] if item["state"] == "pending"]
        if not remaining:
            return None
        result = self.preview(
            batch["mode"], batch["network"], Asset(**batch["asset"]), remaining, wallets
        )
        result["request_ids"] = [item["request_id"] for item in remaining]
        return result

    def start(self, batch_id, preview=None):
        with batch_store(self.directory) as store:
            batch = store.get(batch_id)
            if batch["state"] != "paused":
                raise UserError("只有已暂停的批次可以继续。")
            if any(other["state"] == "running" for other in store.history()):
                raise UserError("另一个批次正在执行，请先暂停或结束。")
            if preview is not None:
                if preview["errors"]:
                    raise UserError("请修正剩余任务的错误。")
                if any(preview[key] != batch[key] for key in ("mode", "network", "asset")):
                    raise UserError("预览与批次的网络、资产或模式不一致。")
                pending = [item for item in batch["items"] if item["state"] == "pending"]
                ids = [item["request_id"] for item in pending]
                if ids != preview.get("request_ids", ids) or len(pending) != len(preview["items"]):
                    raise UserError("剩余清单已变化，请重新预览。")
                for item, approved in zip(pending, preview["items"]):
                    if any(item[key] != approved[key] for key in ("sender", "recipient", "amount")):
                        raise UserError("预览与批次内容不一致。")
                with store.db:
                    for item, approved in zip(pending, preview["items"]):
                        store.db.execute(
                            "UPDATE batch_items SET fee_cap=?,error='' WHERE request_id=?",
                            (approved["fee_cap"], item["request_id"]),
                        )
            self.stops[batch_id] = Event()
            store.state(batch_id, "running")

    def pause(self, batch_id, reason="用户暂停后续发送"):
        self.stops.setdefault(batch_id, Event()).set()
        with batch_store(self.directory) as store:
            store.state(batch_id, "paused", reason, only_running=True)

    def cancel(self, batch_id):
        self.stops.setdefault(batch_id, Event()).set()
        with batch_store(self.directory) as store:
            store.cancel(batch_id)

    def step(self, batch_id, vault, generation=None):
        # A tick performs at most one network transaction; it never runs a blocking wait loop.
        if not self.execution_lock.acquire(blocking=False):
            return self.get(batch_id)
        try:
            batch = self.get(batch_id)
            for item in batch["items"]:
                if item["state"] in {"signed", "submitted", "mined", "unknown"}:
                    try:
                        result = self.backend.refresh(item["request_id"])
                        if result["state"] == "confirmed" and item["error"]:
                            with batch_store(self.directory) as tasks:
                                tasks.error(item["request_id"], "")
                        if result["state"] == "unknown":
                            # A missing receipt is normal while a known transaction is in the mempool.
                            # Keep waiting without creating or rebroadcasting another transaction.
                            with self.backend.service(batch["network"]) as service:
                                try:
                                    transaction = service.web3.eth.get_transaction(item["tx_hash"])
                                except TransactionNotFound:
                                    transaction = None
                                if (
                                    transaction is not None
                                    and transaction.get("blockNumber") is None
                                    and Web3.to_hex(transaction["hash"]).lower()
                                    == item["tx_hash"].lower()
                                ):
                                    service.store.update(item["tx_hash"], "submitted")
                        if result["state"] == "failed":
                            with batch_store(self.directory) as tasks:
                                tasks.error(
                                    item["request_id"],
                                    "链上执行失败，Gas 可能已扣除；不会自动重新付款。",
                                )
                            self.pause(batch_id, "有交易链上执行失败，请核对后确认继续剩余任务。")
                    except OperationCancelled:
                        raise
                    except Exception as error:  # noqa: BLE001 - never infer failure from RPC errors
                        self.pause(batch_id, friendly_error(error))
                        return self.get(batch_id)
            batch = self.get(batch_id)
            if batch["state"] != "running":
                return batch
            unresolved = next(
                (
                    item
                    for item in batch["items"]
                    if item["state"] not in {"confirmed", "failed", "skipped"}
                ),
                None,
            )
            if unresolved is None:
                with batch_store(self.directory) as store:
                    state = (
                        "completed_with_errors"
                        if any(item["state"] == "failed" for item in batch["items"])
                        else "completed"
                    )
                    store.state(batch_id, state, only_running=True)
                return self.get(batch_id)
            if unresolved["state"] in {"signed", "unknown"}:
                self.pause(batch_id, "发送结果不明确，请查询状态，必要时重播原交易；不要重新付款。")
                return self.get(batch_id)
            if unresolved["state"] in {"submitted", "mined"}:
                return batch
            if not vault.unlocked or generation is None:
                self.pause(batch_id, "钱包库已锁定，请解锁并确认继续。")
                return self.get(batch_id)
            stop = StopSignal(self.backend.cancelled, self.stops.setdefault(batch_id, Event()))
            try:
                check_cancelled(stop)
                with (
                    self.backend.service(batch["network"]) as service,
                    batch_store(self.directory) as tasks,
                ):
                    request = TransferRequest(
                        unresolved["sender"],
                        unresolved["recipient"],
                        unresolved["amount"],
                        batch["asset"]["contract"],
                    )
                    prepared = service.prepare(request)
                    if int(prepared.preview["max_execution_fee_wei"]) > int(unresolved["fee_cap"]):
                        raise UserError("执行手续费超过确认上限，请重新预览剩余任务。")
                    prepared.preview.update(
                        network=batch["network"],
                        symbol=batch["asset"]["symbol"],
                        batch_id=batch_id,
                        batch_position=unresolved["position"],
                    )
                    signer = BatchSigner(
                        VaultSigner(vault, request.sender, generation, stop), tasks, batch_id, stop
                    )
                    result = service.submit(unresolved["request_id"], prepared, signer)
                    if result["state"] == "unknown":
                        self.pause(batch_id, "广播结果不明确，请核对状态或重播原交易。")
            except OperationCancelled:
                self.pause(batch_id, "已停止后续签名；请核对已发送交易。")
            except Exception as error:  # noqa: BLE001 - a row error must pause, never retry payment
                record_exception("批量转账暂停", error)
                with batch_store(self.directory) as store:
                    store.error(unresolved["request_id"], friendly_error(error))
                self.pause(batch_id, friendly_error(error))
            return self.get(batch_id)
        finally:
            self.execution_lock.release()
