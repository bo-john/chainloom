import json
import re
import time
from dataclasses import dataclass

from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound

from .config import Network
from .diagnostics import record_exception, record_operation
from .errors import UserError
from .storage import Store

ERC20_ABI = [
    {
        "type": "function",
        "name": "decimals",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint8"}],
    },
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "transfer",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "outputs": [{"type": "bool"}],
    },
]


def address(value: str) -> str:
    if not Web3.is_address(value):
        raise UserError("Invalid EVM address")
    body = value.removeprefix("0x")
    if not (body.islower() or body.isupper() or body.isdigit()) and not Web3.is_checksum_address(
        value
    ):
        raise UserError("Invalid mixed-case address checksum")
    result = Web3.to_checksum_address(value)
    if int(result, 16) == 0:
        raise UserError("Zero address is not allowed")
    return result


def amount_units(amount: str, decimals: int) -> int:
    # String arithmetic avoids Decimal's context precision and all floating-point rounding.
    if len(amount) > 400 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", amount):
        raise UserError("Amount must be a positive plain decimal string")
    whole, _, fraction = amount.partition(".")
    if len(fraction) > decimals:
        raise UserError(f"Amount exceeds the asset's {decimals} decimal places")
    units = int(whole) * 10**decimals + int(fraction.ljust(decimals, "0") or "0")
    if not 0 < units < 2**256:
        raise UserError("Amount must be positive and fit uint256")
    return units


def format_units(units: int, decimals: int = 18) -> str:
    if not decimals:
        return str(units)
    value = str(units).rjust(decimals + 1, "0")
    return (value[:-decimals] + "." + value[-decimals:]).rstrip("0").rstrip(".")


@dataclass(frozen=True)
class TransferRequest:
    sender: str
    recipient: str
    amount: str
    token: str | None = None

    def normalized(self):
        # Validate syntax before producing a canonical idempotency fingerprint.
        if (
            len(self.amount) > 400
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", self.amount)
            or not any(char in "123456789" for char in self.amount)
        ):
            raise UserError("Amount must be a positive plain decimal string")
        whole, _, fraction = self.amount.partition(".")
        normalized_amount = str(int(whole))
        if fraction.rstrip("0"):
            normalized_amount += "." + fraction.rstrip("0")
        return TransferRequest(
            address(self.sender),
            address(self.recipient),
            normalized_amount,
            address(self.token) if self.token else None,
        )

    def intent(self, chain_id: int) -> str:
        request = self.normalized()
        return json.dumps({"chain_id": chain_id, **request.__dict__}, sort_keys=True)


@dataclass
class PreparedTransfer:
    request: TransferRequest
    transaction: dict
    preview: dict
    created: float


class TransferService:
    def __init__(self, web3: Web3, network: Network, store: Store):
        self.web3, self.network, self.store = web3, network, store

    def check_chain(self):
        if self.web3.eth.chain_id != self.network.chain_id:
            raise UserError("RPC chainId does not match the configured network")

    def existing(self, request_id: str, request: TransferRequest):
        record = self.store.get(request_id)
        if record and record["intent"] != request.intent(self.network.chain_id):
            raise UserError("Request ID already belongs to different transfer parameters")
        return record

    def _asset(self, request):
        if not request.token:
            return 18, None
        if not self.web3.eth.get_code(request.token):
            raise UserError("Token address has no contract code")
        contract = self.web3.eth.contract(address=request.token, abi=ERC20_ABI)
        decimals = contract.functions.decimals().call()
        if not isinstance(decimals, int) or not 0 <= decimals <= 255:
            raise UserError("Invalid ERC-20 decimals")
        return decimals, contract

    def _balances_and_simulation(self, request, transaction):
        decimals, contract = self._asset(request)
        units = amount_units(request.amount, decimals)
        if (
            contract
            and contract.functions.balanceOf(request.sender).call(block_identifier="pending")
            < units
        ):
            raise UserError("Insufficient token balance")
        fee_price = transaction.get("maxFeePerGas", transaction.get("gasPrice", 0))
        required = transaction["value"] + transaction["gas"] * fee_price
        if self.web3.eth.get_balance(request.sender, "pending") < required:
            raise UserError("Insufficient native balance for amount and maximum execution gas fee")
        result = bytes(self.web3.eth.call(transaction, block_identifier="pending"))
        if contract and result not in (b"", (1).to_bytes(32, "big")):
            raise UserError("ERC-20 transfer simulation did not return success")
        return units

    def prepare(self, request: TransferRequest) -> PreparedTransfer:
        self.check_chain()
        # Preserve original decimal precision for validation (including trailing zeroes).
        decimals, _ = self._asset(request.normalized())
        units = amount_units(request.amount, decimals)
        request = request.normalized()
        tx = {
            "chainId": self.network.chain_id,
            "from": request.sender,
            "to": request.recipient,
            "value": units,
            "data": "0x",
            "nonce": self.web3.eth.get_transaction_count(request.sender, "pending"),
        }
        if request.token:
            contract = self.web3.eth.contract(address=request.token, abi=ERC20_ABI)
            if (
                contract.functions.balanceOf(request.sender).call(block_identifier="pending")
                < units
            ):
                raise UserError("Insufficient token balance")
            tx.update(
                to=request.token,
                value=0,
                data=contract.encode_abi("transfer", args=[request.recipient, units]),
            )
        if self.web3.eth.get_balance(request.sender, "pending") <= tx["value"]:
            raise UserError("Insufficient native balance for amount and gas fee")
        block = self.web3.eth.get_block("latest")
        tx.update(self._fee_fields(block))
        tx["gas"] = self._gas_limit(tx, block)
        self._balances_and_simulation(request, tx)
        return self._prepared(request, tx, decimals, units)

    def _fee_fields(self, block):
        if block.get("baseFeePerGas") is not None:
            priority = self.web3.eth.max_priority_fee
            return {
                "maxPriorityFeePerGas": priority,
                "maxFeePerGas": 2 * block["baseFeePerGas"] + priority,
            }
        return {"gasPrice": self.web3.eth.gas_price}

    def _gas_limit(self, tx, block):
        estimate = self.web3.eth.estimate_gas(
            {key: value for key, value in tx.items() if key != "gas"}, block_identifier="pending"
        )
        limit = (estimate * 120 + 99) // 100
        if limit > block["gasLimit"]:
            raise UserError("Estimated gas plus margin exceeds block gas limit")
        return limit

    def prepare_collection(self, sender, recipient, token=None, extra_reserve=0):
        """Calculate a fixed transferable amount; never sign or broadcast here."""
        if type(extra_reserve) is not int or not 0 <= extra_reserve < 2**256:
            raise UserError("额外预留必须是非负的原生币数量。")
        self.check_chain()
        request = TransferRequest(sender, recipient, "1", token).normalized()
        if request.sender == request.recipient:
            raise UserError("发送钱包与归集地址相同，请从清单中移除此行。")
        decimals, contract = self._asset(request)
        native = self.web3.eth.get_balance(request.sender, "pending")
        available = (
            contract.functions.balanceOf(request.sender).call(block_identifier="pending")
            if contract
            else native
        )
        if available <= 0:
            raise UserError("该钱包的所选资产余额为 0，没有可归集数量。")
        tx = {
            "chainId": self.network.chain_id,
            "from": request.sender,
            "to": request.token or request.recipient,
            "value": 0 if contract else 1,
            "data": contract.encode_abi("transfer", args=[request.recipient, available])
            if contract
            else "0x",
            "nonce": self.web3.eth.get_transaction_count(request.sender, "pending"),
        }
        block = self.web3.eth.get_block("latest")
        tx.update(self._fee_fields(block))
        price = tx.get("maxFeePerGas", tx.get("gasPrice", 0))
        tx["gas"] = self._gas_limit(tx, block)
        # A recipient contract can require different gas for the final value.
        # Grow the reservation until the final transfer fits; never assume 21,000 gas.
        for _ in range(5):
            fee = tx["gas"] * price
            if native < fee + extra_reserve:
                raise UserError("原生币余额不足以支付执行手续费和额外预留。")
            units = available if contract else native - fee - extra_reserve
            if units <= 0:
                raise UserError("扣除手续费和额外预留后，没有可归集数量。")
            if not contract:
                tx["value"] = units
            required = self._gas_limit(tx, block)
            if required <= tx["gas"]:
                break
            tx["gas"] = required
        else:
            raise UserError("归集手续费估算未稳定，请稍后重试或手动填写数量。")
        request = TransferRequest(
            request.sender, request.recipient, format_units(units, decimals), request.token
        )
        self._balances_and_simulation(request, tx)
        if self.web3.eth.get_balance(request.sender, "pending") < tx["value"] + fee + extra_reserve:
            raise UserError("计算期间钱包余额已变化，请重新计算。")
        prepared = self._prepared(request, tx, decimals, units)
        prepared.preview["collection"] = {
            "balance_units": str(available),
            "native_balance_wei": str(native),
            "extra_reserve_wei": str(extra_reserve),
        }
        return prepared

    def _prepared(self, request, tx, decimals, units):
        fee_price = tx.get("maxFeePerGas", tx.get("gasPrice"))
        preview = {
            "network": self.network.name,
            "chain_id": self.network.chain_id,
            "from": request.sender,
            "to": request.recipient,
            "asset": request.token or self.network.symbol,
            "amount": request.amount,
            "amount_units": str(units),
            "decimals": decimals,
            "nonce": tx["nonce"],
            "gas_limit": tx["gas"],
            "fee_mode": "eip1559" if "maxFeePerGas" in tx else "legacy",
            "max_execution_fee_wei": str(tx["gas"] * fee_price),
            "fee_fields_wei": {
                key: str(tx[key])
                for key in ("gasPrice", "maxFeePerGas", "maxPriorityFeePerGas")
                if key in tx
            },
        }
        return PreparedTransfer(request, tx, preview, time.monotonic())

    def submit(self, request_id: str, prepared: PreparedTransfer, signer) -> dict:
        if not request_id.strip() or len(request_id) > 128:
            raise UserError("Request ID must contain 1–128 characters")
        with self.store.lock():
            previous = self.existing(request_id, prepared.request)
            if previous:
                record_operation(
                    "重复请求返回已有转账记录", 请求编号=request_id, 交易哈希=previous["tx_hash"]
                )
                return self.public_record(previous)
            active = self.store.active(self.network.chain_id, signer.address)
            if active:
                raise UserError(
                    f"Resolve the pending transfer in transaction history first: {active['request_id']}"
                )
            self.validate_prepared(prepared, signer.address)
            signed = signer.sign_transaction(prepared.transaction)
            tx_hash = Web3.to_hex(signed.hash)
            self.store.save(
                request_id,
                self.network.chain_id,
                signer.address,
                prepared.request.intent(self.network.chain_id),
                tx_hash,
                Web3.to_hex(signed.raw_transaction),
                prepared.preview,
            )
            return self._broadcast(self.store.get(request_id))

    def validate_prepared(self, prepared: PreparedTransfer, sender: str):
        """Recheck a quote before signing with the selected local wallet."""
        self.check_chain()
        if address(sender) != prepared.request.sender:
            raise UserError("Signing wallet address does not match the selected sender")
        if time.monotonic() - prepared.created > 120:
            raise UserError("Preview expired; prepare a fresh transfer preview")
        tx = prepared.transaction
        expected_to = prepared.request.token or prepared.request.recipient
        decimals, contract = self._asset(prepared.request)
        units = amount_units(prepared.request.amount, decimals)
        expected_data = (
            contract.encode_abi("transfer", args=[prepared.request.recipient, units])
            if contract
            else "0x"
        )
        if (
            tx["chainId"] != self.network.chain_id
            or tx["from"] != sender
            or tx["to"] != expected_to
            or tx["value"] != (0 if contract else units)
            or tx["data"] != expected_data
            or tx["nonce"] != prepared.preview["nonce"]
            or tx["gas"] != prepared.preview["gas_limit"]
            or {
                key: str(tx[key])
                for key in ("gasPrice", "maxFeePerGas", "maxPriorityFeePerGas")
                if key in tx
            }
            != prepared.preview["fee_fields_wei"]
            or set(tx)
            - {
                "chainId",
                "from",
                "to",
                "value",
                "data",
                "nonce",
                "gas",
                "gasPrice",
                "maxFeePerGas",
                "maxPriorityFeePerGas",
            }
        ):
            raise UserError("Transaction parameters changed since preview")
        if self.web3.eth.get_transaction_count(sender, "pending") != tx["nonce"]:
            raise UserError("Nonce changed since preview; prepare a fresh transfer preview")
        block = self.web3.eth.get_block("latest")
        if "maxFeePerGas" in tx and block.get("baseFeePerGas", 0) > tx["maxFeePerGas"]:
            raise UserError("Base fee exceeds preview; prepare a fresh transfer preview")
        self._balances_and_simulation(prepared.request, tx)
        if self.web3.eth.estimate_gas(tx, block_identifier="pending") > tx["gas"]:
            raise UserError(
                "Gas requirement increased since preview; prepare a fresh transfer preview"
            )

    @staticmethod
    def public_record(record):
        return {key: value for key, value in record.items() if key not in {"raw_tx", "intent"}}

    def _broadcast(self, record):
        record_operation(
            "签名交易已保存，开始广播",
            请求编号=record["request_id"],
            交易哈希=record["tx_hash"],
            链编号=record["chain_id"],
        )
        try:
            received = self.web3.eth.send_raw_transaction(HexBytes(record["raw_tx"]))
            if Web3.to_hex(received) != record["tx_hash"]:
                raise UserError("RPC returned a different transaction hash")
        except Exception as error:  # noqa: BLE001 - every broadcast error is ambiguous
            record_exception("广播交易（发送结果不明确）", error)
            # Any transport/RPC failure after signing is ambiguous. Never create a replacement.
            self.store.update(record["tx_hash"], "unknown")
            result = self.public_record(self.store.get(record["request_id"]))
            result["message"] = (
                "Broadcast result uncertain; query status or rebroadcast this request"
            )
            return result
        self.store.update(record["tx_hash"], "submitted")
        record_operation(
            "交易广播已被节点接受", 请求编号=record["request_id"], 交易哈希=record["tx_hash"]
        )
        return self.public_record(self.store.get(record["request_id"]))

    def rebroadcast(self, request_id: str):
        with self.store.lock():
            self.check_chain()
            record = self.store.get(request_id)
            if not record or record["chain_id"] != self.network.chain_id:
                raise UserError("Request not found on this network")
            status = self.status(record["tx_hash"])
            if status["state"] in {"confirmed", "failed", "mined"}:
                record_operation(
                    "原交易已上链，跳过重播", 请求编号=request_id, 交易哈希=record["tx_hash"]
                )
                return status
            record_operation("重播原签名交易", 请求编号=request_id, 交易哈希=record["tx_hash"])
            return self._broadcast(record)

    def status(self, tx_hash: str):
        self.check_chain()
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash):
            raise UserError("Transaction hash must be 0x followed by 64 hexadecimal characters")
        tx_hash = tx_hash.lower()
        try:
            receipt = self.web3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            # Missing does not mean failed: an RPC may have lost its mempool or lag behind.
            self.store.update(tx_hash, "unknown")
            return {"tx_hash": tx_hash, "state": "unknown", "confirmations": 0}
        block = self.web3.eth.get_block(receipt["blockNumber"])
        if block["hash"] != receipt["blockHash"]:
            self.store.update(tx_hash, "unknown")
            return {"tx_hash": tx_hash, "state": "unknown", "confirmations": 0}
        confirmations = max(0, self.web3.eth.block_number - receipt["blockNumber"] + 1)
        if confirmations < self.network.confirmations:
            state = "mined"
        else:
            state = "confirmed" if receipt["status"] == 1 else "failed"
        self.store.update(tx_hash, state)
        result = {
            "tx_hash": tx_hash,
            "state": state,
            "receipt_status": receipt["status"],
            "confirmations": confirmations,
            "required_confirmations": self.network.confirmations,
            "block_number": receipt["blockNumber"],
            "gas_used": receipt["gasUsed"],
            "execution_fee_wei": str(receipt["gasUsed"] * receipt["effectiveGasPrice"]),
        }
        if self.network.explorer:
            result["explorer_url"] = self.network.explorer.rstrip("/") + "/tx/" + tx_hash
        return result
