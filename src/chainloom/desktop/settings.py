import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ..config import Network
from ..diagnostics import record_exception, record_operation, rpc_endpoint_summary
from ..errors import UserError
from ..transfer import address


@dataclass(frozen=True)
class Asset:
    symbol: str
    contract: str | None = None


@dataclass(frozen=True)
class NetworkInfo:
    name: str
    title: str
    chain_id: int
    symbol: str
    rpcs: tuple[str, ...]
    explorer: str
    confirmations: int
    assets: tuple[Asset, ...]
    poa: bool = False


NETWORKS = {
    "ethereum": NetworkInfo(
        "ethereum",
        "Ethereum",
        1,
        "ETH",
        ("https://ethereum-rpc.publicnode.com", "https://eth.drpc.org"),
        "https://etherscan.io",
        12,
        (
            Asset("ETH"),
            Asset("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7"),
            Asset("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
        ),
    ),
    "bsc": NetworkInfo(
        "bsc",
        "BNB Smart Chain",
        56,
        "BNB",
        ("https://bsc-rpc.publicnode.com", "https://bsc-dataseed.binance.org"),
        "https://bscscan.com",
        15,
        (
            Asset("BNB"),
            Asset("USDT", "0x55d398326f99059fF775485246999027B3197955"),
            Asset("USDC", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"),
        ),
        True,
    ),
    "base": NetworkInfo(
        "base",
        "Base",
        8453,
        "ETH",
        ("https://mainnet.base.org", "https://base-rpc.publicnode.com"),
        "https://basescan.org",
        12,
        (Asset("ETH"), Asset("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")),
    ),
    "xlayer": NetworkInfo(
        "xlayer",
        "X Layer",
        196,
        "OKB",
        ("https://rpc.xlayer.tech", "https://xlayerrpc.okx.com"),
        "https://www.okx.com/web3/explorer/xlayer",
        12,
        (Asset("OKB"),),
    ),
}

# Kept only to render old records; retired networks cannot create or rebroadcast payments.
HISTORY_NETWORKS = {
    **NETWORKS,
    "sepolia": NetworkInfo(
        "sepolia",
        "Sepolia（已移除）",
        11155111,
        "ETH",
        (),
        "https://sepolia.etherscan.io",
        2,
        (Asset("ETH"),),
    ),
}


@dataclass(frozen=True)
class Preferences:
    network: str = "ethereum"
    rpc_overrides: dict[str, str] = field(default_factory=dict)
    custom_assets: dict[str, list[dict]] = field(default_factory=dict)

    def validate(self):
        if self.network not in NETWORKS:
            raise UserError("不支持的网络配置。")
        for name, rpc in self.rpc_overrides.items():
            if name not in NETWORKS:
                raise UserError("不支持的网络配置。")
            parsed = urlsplit(rpc)
            local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if not parsed.netloc or (
                parsed.scheme != "https" and not (local and parsed.scheme == "http")
            ):
                raise UserError("RPC 请使用 HTTPS 地址；本机测试节点可以使用 HTTP。")
        for name, assets in self.custom_assets.items():
            if name not in NETWORKS or not isinstance(assets, list):
                raise UserError("代币配置格式不正确。")
            for item in assets:
                if not re.fullmatch(r"[A-Za-z0-9._-]{1,16}", item["symbol"]):
                    raise UserError("代币简称请使用 1–16 位字母、数字或 . _ -。")
                address(item["contract"])
        return self

    def assets(self, name):
        return NETWORKS[name].assets + tuple(
            Asset(**entry) for entry in self.custom_assets.get(name, [])
        )

    def networks(self, name):
        info = NETWORKS[name]
        urls = (self.rpc_overrides[name],) if self.rpc_overrides.get(name) else info.rpcs
        return tuple(
            Network(
                info.name,
                info.chain_id,
                url,
                info.symbol,
                info.confirmations,
                info.poa,
                info.explorer,
            )
            for url in urls
        )


def load_preferences(directory: Path) -> Preferences:
    path = directory / "settings.json"
    if path.exists():
        try:
            content = path.read_bytes()
            data = json.loads(content)
            if not isinstance(data, dict):
                raise TypeError("设置文件必须是对象")
            # Drop the retired connection setting, including invalid old values.
            removed_connection_setting = "project_id" in data
            data.pop("project_id", None)
            migrated = data.get("network") == "sepolia"
            if migrated:
                data["network"] = "ethereum"
            for key in ("rpc_overrides", "custom_assets"):
                values = data.get(key, {})
                if isinstance(values, dict) and "sepolia" in values:
                    migrated = True
                    values.pop("sepolia")
            preferences = Preferences(**data).validate()
            if migrated:
                backup = directory / "settings.before-sepolia-removal.json"
                try:
                    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                save_preferences(directory, preferences)
                record_operation(
                    "移除Sepolia旧设置", 当前默认网络=preferences.network, 旧设置备份=backup.name
                )
            if removed_connection_setting and not migrated:
                save_preferences(directory, preferences)
            return preferences
        except (ValueError, KeyError, TypeError):
            raise UserError(
                "本地设置无法读取。请备份并检查 settings.json，交易记录不受影响。"
            ) from None
    return Preferences().validate()


def save_preferences(directory: Path, preferences: Preferences):
    preferences.validate()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    previous = {}
    settings_path = directory / "settings.json"
    if settings_path.exists():
        try:
            previous = json.loads(settings_path.read_text())
            if not isinstance(previous, dict):
                previous = {}
        except (ValueError, OSError) as error:
            record_exception("读取修改前的设置", error)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix="settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(asdict(preferences), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, settings_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    changes = {}
    if previous.get("network") != preferences.network:
        changes["默认网络"] = {"修改前": previous.get("network"), "修改后": preferences.network}
    old_rpcs = previous.get("rpc_overrides", {})
    if not isinstance(old_rpcs, dict):
        old_rpcs = {}
    for name in NETWORKS:
        old, new = old_rpcs.get(name, ""), preferences.rpc_overrides.get(name, "")
        if old != new:
            changes["RPC节点：" + name] = {
                "修改前": rpc_endpoint_summary(old),
                "修改后": rpc_endpoint_summary(new),
                "配置内容有变更": True,
            }
    if previous.get("custom_assets", {}) != preferences.custom_assets:
        changes["自定义资产"] = {
            "修改前": previous.get("custom_assets", {}),
            "修改后": preferences.custom_assets,
        }
    record_operation("设置保存成功", 修改内容=changes)
