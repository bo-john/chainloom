import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound

from ..diagnostics import LOGGER, record_exception, record_operation
from ..errors import UserError
from ..storage import Store
from ..transfer import (
    ERC20_ABI,
    PreparedTransfer,
    TransferRequest,
    TransferService,
    address,
    format_units,
)
from .rpc import OperationCancelled, check_cancelled, connect_desktop
from .settings import HISTORY_NETWORKS, NETWORKS, Asset, Preferences


def friendly_error(error: Exception) -> str:
    if isinstance(error, UserError):
        message = str(error)
        translations = {
            "Invalid EVM address": "收款地址格式不正确，请粘贴完整的 0x 地址。",
            "Invalid mixed-case": "地址大小写校验未通过，请重新核对原始地址。",
            "Zero address": "不能向零地址转账。",
            "Amount must": "请输入大于 0 的数量，使用普通小数格式。",
            "Amount exceeds": "数量的小数位超过该资产支持的精度。",
            "Insufficient native": "原生币余额不足以完成转账并支付网络手续费，请先补充余额。",
            "Insufficient token": "代币余额不足，请减少转账数量。",
            "Token address has no": "该地址在当前网络上不是代币合约，请核对网络和地址。",
            "ERC-20 transfer simulation": "代币转账模拟失败，当前无法发送。",
            "RPC chainId": "节点返回了错误的网络，已停止操作。请在设置中检查 RPC。",
            "Nonce changed": "钱包已有其他交易，请重新预览后再发送。",
            "Preview expired": "转账预览已过期，请重新预估费用。",
            "Base fee exceeds": "网络费用已上涨，请重新预估。",
            "Gas requirement increased": "所需网络费用已变化，请重新预估。",
            "Another transfer": "另一个转账操作正在执行，请稍后再试。",
            "Transaction parameters changed": "转账参数已变化，请重新预览。",
        }
        for prefix, text in translations.items():
            if message.startswith(prefix):
                return text
        if any("\u4e00" <= char <= "\u9fff" for char in message):
            return message
    # Never reflect RPC URLs, API keys, session keys, or arbitrary remote messages.
    return "操作暂时未完成。请检查网络连接、钱包状态或 RPC 设置；如已请求发送，请先查看交易记录。"


@dataclass(frozen=True)
class Quote:
    network: str
    asset: Asset
    prepared: PreparedTransfer


@dataclass
class VaultSigner:
    vault: object
    address: str
    generation: int
    cancelled: Event

    def sign_transaction(self, transaction):
        check_cancelled(self.cancelled)
        LOGGER.info("开始解密并签名本地钱包交易")
        signed = self.vault.sign_transaction(
            self.address, transaction, self.generation, self.cancelled
        )
        LOGGER.info("本地钱包交易签名完成")
        return signed


class DesktopService:
    """Synchronous operations. The GUI invokes these on workers, each with its own SQLite connection."""

    def __init__(self, directory: Path, preferences: Preferences, connector=None, cancelled=None):
        self.directory = directory
        self.preferences = preferences
        self.connector = connector
        self.cancelled = cancelled if cancelled is not None else Event()

    def web3(self, name):
        check_cancelled(self.cancelled)
        last = None
        for network in self.preferences.networks(name):
            try:
                web3 = (
                    self.connector(network)
                    if self.connector
                    else connect_desktop(network, self.cancelled)
                )
                check_cancelled(self.cancelled)
                return web3, network
            except OperationCancelled:
                raise
            except Exception as error:  # noqa: BLE001 - try the next configured read endpoint
                record_exception("RPC连接及备用节点切换", error, level=logging.WARNING)
                last = error
        raise last or UserError("没有可用的 RPC 节点。")

    @contextmanager
    def service(self, name):
        web3, network = self.web3(name)
        store = Store(self.directory)
        try:
            yield TransferService(web3, network, store)
        finally:
            store.close()

    def balance(self, name, sender, asset):
        web3, _ = self.web3(name)
        sender = address(sender)
        native = web3.eth.get_balance(sender, "pending")
        if asset.contract:
            contract = web3.eth.contract(address=address(asset.contract), abi=ERC20_ABI)
            decimals = contract.functions.decimals().call()
            units = contract.functions.balanceOf(sender).call(block_identifier="pending")
        else:
            decimals, units = 18, native
        return {
            "amount": format_units(units, decimals),
            "native": format_units(native),
            "decimals": decimals,
        }

    def bulk_balances(
        self, name, senders, assets, cancelled, progress=None, *, include_native=True
    ):
        """Read public balances at one block; failures never masquerade as zero."""
        if name not in NETWORKS or not 1 <= len(senders) <= 1000:
            raise UserError("请选择网络及 1–1000 个钱包进行查询。")

        def check():
            check_cancelled(self.cancelled)
            check_cancelled(cancelled)

        check()
        senders = list(dict.fromkeys(address(sender) for sender in senders))
        web3, _ = self.web3(name)
        block = web3.eth.block_number
        tokens = {}
        for asset in assets:
            if not asset.contract or asset.contract in tokens:
                continue
            check()
            try:
                token = web3.eth.contract(address=address(asset.contract), abi=ERC20_ABI)
                decimals = token.functions.decimals().call(block_identifier=block)
                if type(decimals) is not int or not 0 <= decimals <= 255:
                    raise UserError("代币精度不正确。")
                tokens[asset.contract] = (token, decimals)
            except OperationCancelled:
                raise
            except Exception as error:  # noqa: BLE001 - retain native/other token results
                record_exception("批量余额查询：读取代币精度", error)
                tokens[asset.contract] = None
        results = []
        for sender in senders:
            check()
            balances, errors = {}, {}
            for contract in ((None,) if include_native else ()) + tuple(tokens):
                check()
                try:
                    if contract is None:
                        units = web3.eth.get_balance(sender, block)
                        decimals = 18
                    elif tokens[contract] is None:
                        errors[contract] = "无法读取代币精度，请检查合约及节点。"
                        continue
                    else:
                        token, decimals = tokens[contract]
                        units = token.functions.balanceOf(sender).call(block_identifier=block)
                    balances[contract] = format_units(units, decimals)
                except OperationCancelled:
                    raise
                except Exception as error:  # noqa: BLE001 - each cell can fail independently
                    record_exception("批量余额查询：读取钱包余额", error)
                    errors[contract] = "查询失败，请检查网络节点或代币合约后重试。"
            check()
            result = {"address": sender, "balances": balances, "errors": errors, "block": block}
            results.append(result)
            if progress:
                progress(result)
        return results

    def inspect_token(self, name, symbol, contract):
        asset = Asset(symbol, address(contract))
        web3, _ = self.web3(name)
        if not web3.eth.get_code(asset.contract):
            raise UserError("该地址在所选网络上没有代币合约。")
        token = web3.eth.contract(address=asset.contract, abi=ERC20_ABI)
        decimals = token.functions.decimals().call()
        if not 0 <= decimals <= 255:
            raise UserError("代币精度不正确。")
        return asset

    def prepare(self, name, sender, recipient, amount, asset):
        if name not in NETWORKS:
            raise UserError("该网络已移除或尚未支持，不能创建转账。")
        with self.service(name) as service:
            request = TransferRequest(sender, recipient, amount, asset.contract)
            if service.store.active(service.network.chain_id, address(sender)):
                raise UserError("此钱包还有结果待确认的交易，请先到交易记录中查询状态。")
            prepared = service.prepare(request)
            record_operation(
                "转账预览完成",
                网络=name,
                发送地址=prepared.request.sender,
                收款地址=prepared.request.recipient,
                数量=prepared.request.amount,
                资产=asset.symbol,
                代币合约=asset.contract,
                执行手续费上限_wei=prepared.preview["max_execution_fee_wei"],
            )
            return Quote(name, asset, prepared)

    def begin(self, quote: Quote, sender: str):
        if quote.network not in NETWORKS:
            raise UserError("该网络已移除或尚未支持，不能创建转账。")
        with self.service(quote.network) as service, service.store.lock():
            if service.store.active(service.network.chain_id, address(sender)):
                raise UserError("此钱包还有结果待确认的交易，请先查看交易记录。")
            service.validate_prepared(quote.prepared, sender)
            request_id = "wallet-" + uuid.uuid4().hex
            details = {**quote.prepared.preview, "symbol": quote.asset.symbol}
            service.store.wallet_save(
                request_id, quote.network, address(sender), quote.prepared.transaction, details
            )
            tx = {
                key: hex(value) if isinstance(value, int) else value
                for key, value in quote.prepared.transaction.items()
            }
            return {
                "request_id": request_id,
                "transaction": tx,
                "chain_id": service.network.chain_id,
            }

    def submit_local(self, quote, vault, generation, request_id):
        """Persist signed bytes before broadcasting; a retry never creates a new signature."""
        if quote.network not in NETWORKS:
            raise UserError("该网络已移除或尚未支持，不能创建转账。")
        LOGGER.info("开始连接节点并复查转账参数")
        with self.service(quote.network) as service:
            quote.prepared.preview.update(network=quote.network, symbol=quote.asset.symbol)
            signer = VaultSigner(vault, quote.prepared.request.sender, generation, self.cancelled)
            result = service.submit(request_id, quote.prepared, signer)
            return self._local_record(result)

    @staticmethod
    def _local_record(record):
        network = record["details"]["network"]
        if network not in HISTORY_NETWORKS:
            network = next(
                (
                    name
                    for name, info in HISTORY_NETWORKS.items()
                    if info.chain_id == record["chain_id"]
                ),
                None,
            )
        return {**TransferService.public_record(record), "network": network, "source": "local"}

    def rebroadcast(self, request_id):
        store = Store(self.directory)
        try:
            record = store.get(request_id)
            if not record:
                raise UserError("未找到本地签名的转账记录。")
            network = self._local_record(record)["network"]
            if network not in NETWORKS:
                raise UserError("该网络已移除，历史记录仅供查看，不能重播交易。")
        finally:
            store.close()
        with self.service(network) as service:
            service.rebroadcast(request_id)
        return self.refresh(request_id)

    def wallet_error(self, request_id, rejected=False):
        store = Store(self.directory)
        try:
            record = store.wallet_get(request_id)
            # Never downgrade a hash that has already been received to a rejected request.
            if record and not record["tx_hash"] and record["state"] == "awaiting_wallet":
                store.wallet_update(request_id, "rejected" if rejected else "wallet_unknown")
        finally:
            store.close()

    @staticmethod
    def verify_transaction(web3, record, tx_hash):
        actual = web3.eth.get_transaction(tx_hash)
        expected = record["transaction"]
        data = actual.get("input", actual.get("data", b""))
        matches = (
            Web3.to_hex(actual["hash"]).lower() == tx_hash.lower()
            and actual.get("chainId") == record["chain_id"]
            and actual["from"].lower() == expected["from"].lower()
            and (actual.get("to") or "").lower() == expected["to"].lower()
            and actual["value"] == expected["value"]
            and actual["nonce"] == expected["nonce"]
            and HexBytes(data) == HexBytes(expected["data"])
            and not actual.get("authorizationList")
        )
        if not matches:
            raise UserError("链上交易与转账预览不一致。请在钱包和区块浏览器中核对，切勿重新付款。")

    def refresh(self, request_id):
        store = Store(self.directory)
        try:
            local = store.get(request_id)
            if local:
                network = self._local_record(local)["network"]
                if network not in NETWORKS:
                    return self._local_record(local)
                with self.service(network) as service:
                    service.status(local["tx_hash"])
                return self._local_record(store.get(request_id))
            record = store.wallet_get(request_id)
            if not record:
                raise UserError("未找到对应的本地转账记录。")
            if record["network"] not in NETWORKS:
                return record
            if not record["tx_hash"]:
                return record
            with self.service(record["network"]) as service:
                try:
                    self.verify_transaction(service.web3, record, record["tx_hash"])
                except TransactionNotFound:
                    store.wallet_update(request_id, "verifying")
                except UserError:
                    store.wallet_update(request_id, "mismatched")
                    raise
                else:
                    result = service.status(record["tx_hash"])
                    store.wallet_update(request_id, result["state"])
            return store.wallet_get(request_id)
        finally:
            store.close()

    def history(self):
        store = Store(self.directory)
        try:
            local = [self._local_record(record) for record in store.history(100)]
            local = [record for record in local if record["network"] is not None]
            external = [{**record, "source": "external"} for record in store.wallet_history()]
            return sorted(local + external, key=lambda record: record["created_at"], reverse=True)[
                :100
            ]
        finally:
            store.close()

    def poll(self):
        records = self.history()
        for record in records:
            if self.cancelled.is_set():
                break
            if record["tx_hash"] and record["state"] not in {"confirmed", "failed", "mismatched"}:
                try:
                    self.refresh(record["request_id"])
                except OperationCancelled:
                    break
                except Exception as error:  # noqa: BLE001 - preserve state and log safe diagnostics
                    record_exception("查询交易状态", error, level=logging.WARNING)
                    continue
        return self.history()

    def refresh_selected(self, request_ids):
        requested = set(request_ids)
        records = [record for record in self.history() if record["request_id"] in requested]
        previous = {record["request_id"]: record["state"] for record in records}
        checked, failed, skipped = set(), [], len(requested) - len(records)
        for record in records:
            check_cancelled(self.cancelled)
            if not record["tx_hash"] or record["network"] not in NETWORKS:
                skipped += 1
                continue
            request_id = record["request_id"]
            checked.add(request_id)
            try:
                self.refresh(request_id)
            except OperationCancelled:
                raise
            except Exception as error:  # noqa: BLE001 - report partial failures and continue
                failed.append(request_id)
                record_exception("手动刷新交易状态", error)
        updated_records = self.history()
        changed = sum(
            record["request_id"] in checked and record["state"] != previous[record["request_id"]]
            for record in updated_records
        )
        record_operation(
            "刷新勾选交易的链上状态完成",
            勾选数量=len(requested),
            查询数量=len(checked),
            状态变化数量=changed,
            失败请求编号=failed,
            跳过数量=skipped,
        )
        return {
            "records": updated_records,
            "checked": len(checked),
            "changed": changed,
            "failed": len(failed),
            "skipped": skipped,
        }

    def rebroadcast_selected(self, request_ids):
        requested = set(request_ids)
        records = [record for record in self.history() if record["request_id"] in requested]
        processed, failed, skipped = 0, [], len(requested) - len(records)
        for record in records:
            check_cancelled(self.cancelled)
            if (
                record.get("source") != "local"
                or record["network"] not in NETWORKS
                or record["state"] not in {"signed", "unknown", "submitted"}
            ):
                skipped += 1
                continue
            try:
                self.rebroadcast(record["request_id"])
                processed += 1
            except OperationCancelled:
                raise
            except Exception as error:  # noqa: BLE001 - each signed transaction is independent
                failed.append(record["request_id"])
                record_exception("重播勾选的原交易", error)
        record_operation(
            "勾选交易重播处理完成",
            勾选数量=len(requested),
            已处理数量=processed,
            异常请求编号=failed,
            跳过数量=skipped,
        )
        return {
            "records": self.history(),
            "processed": processed,
            "failed": len(failed),
            "skipped": skipped,
        }

    def explorer(self, record):
        return HISTORY_NETWORKS[record["network"]].explorer + "/tx/" + record["tx_hash"]
