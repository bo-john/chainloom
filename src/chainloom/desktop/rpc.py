from threading import Event

from web3 import HTTPProvider, Web3
from web3.middleware import ExtraDataToPOAMiddleware

from ..errors import UserError


class OperationCancelled(Exception):
    """A background operation stopped because the desktop application is closing."""


def check_cancelled(cancelled: Event):
    if cancelled.is_set():
        raise OperationCancelled()


class DesktopHTTPProvider(HTTPProvider):
    def __init__(self, url: str, cancelled: Event):
        # Disable Web3's implicit retry/backoff loop: the desktop service already
        # has explicit endpoint fallback and must be able to stop between calls.
        super().__init__(
            url, request_kwargs={"timeout": (3, 3)}, exception_retry_configuration=None
        )
        self.cancelled = cancelled

    def _make_request(self, method, request_data):
        check_cancelled(self.cancelled)
        response = super()._make_request(method, request_data)
        check_cancelled(self.cancelled)
        return response


def connect_desktop(network, cancelled: Event):
    check_cancelled(cancelled)
    web3 = Web3(DesktopHTTPProvider(network.rpc_url, cancelled))
    if network.poa:
        web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if web3.eth.chain_id != network.chain_id:
        raise UserError("RPC chainId does not match the configured network")
    return web3
