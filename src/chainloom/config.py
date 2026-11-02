"""Shared network parameters for desktop RPC access and transfers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Network:
    name: str
    chain_id: int
    rpc_url: str
    symbol: str = "ETH"
    confirmations: int = 2
    poa: bool = False
    explorer: str = ""
