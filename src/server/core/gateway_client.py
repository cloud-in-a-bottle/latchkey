import re
from typing import Any

import attr
import httpx

from server.core.config import AppConfig
from server.core.latchkey_runtime import LatchkeyRuntime

PASSWORD_HEADER = "X-Latchkey-Gateway-Password"
PERMISSIONS_OVERRIDE_HEADER = "X-Latchkey-Gateway-Permissions-Override"
ACCOUNT_HEADER = "X-Latchkey-Gateway-Account"

# Headers the gateway treats as its own control channel; a proxied request must never carry a
# consumer's copy of one (see forwardable_request_headers).
GATEWAY_HEADER_PREFIX = "x-latchkey-gateway-"

# The gateway signals a detent policy denial with this exact JSON error body
# (upstream 403s arrive with the upstream's own body instead).
PERMISSION_DENIED_MESSAGE = "Error: Request not permitted by the user."

# Latchkey's AmbiguousAccountError, raised when a service has several stored accounts and the
# request picked none. Matched by message for want of structured error codes (upstream wishlist).
_AMBIGUOUS_ACCOUNT_RE = re.compile(
    r"Multiple accounts are stored for service '(?P<service>[^']*)': (?P<accounts>.*?)\. Specify"
)

PROXY_TIMEOUT_SECONDS = 120.0
RPC_TIMEOUT_SECONDS = 60.0
BROWSER_LOGIN_TIMEOUT_SECONDS = 600.0


class GatewayRpcError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"gateway RPC failed ({status_code}): {message}")
        self.status_code = status_code
        self.message = message


class GatewayClient:
    """Client for the local gateway; every call starts the (idle-stopped) gateway on demand."""

    def __init__(self, config: AppConfig, runtime: LatchkeyRuntime) -> None:
        self._config = config
        self._runtime = runtime
        self._client = httpx.AsyncClient(base_url=config.gateway_url)

    async def close(self) -> None:
        await self._client.aclose()

    def _base_headers(self) -> dict[str, str]:
        return {PASSWORD_HEADER: self._config.gateway_password}

    async def rpc(
        self, command: str, params: dict[str, Any] | None = None, timeout: float = RPC_TIMEOUT_SECONDS
    ) -> Any:
        body: dict[str, Any] = {"command": command}
        if params is not None:
            body["params"] = params
        async with self._runtime.gateway_use():
            response = await self._client.post("/latchkey/", json=body, headers=self._base_headers(), timeout=timeout)
        payload = response.json()
        if response.status_code != 200:
            raise GatewayRpcError(response.status_code, str(payload.get("error", "unknown error")))
        return payload.get("result")

    async def proxy(
        self,
        method: str,
        target_url: str,
        headers: list[tuple[str, str]],
        content: bytes,
        permissions_jwt: str,
        account: str | None = None,
    ) -> httpx.Response:
        """Forward a request through /gateway/<target_url> with a per-consumer permissions override.

        `account` picks which of the service's stored accounts to inject credentials from; with None
        the gateway resolves the single stored account, or fails when there are several. The empty
        string is a choice like any other: latchkey's unnamed default account.
        """
        request_headers = list(headers)
        request_headers.append((PASSWORD_HEADER, self._config.gateway_password))
        request_headers.append((PERMISSIONS_OVERRIDE_HEADER, permissions_jwt))
        if account is not None:
            request_headers.append((ACCOUNT_HEADER, account))
        async with self._runtime.gateway_use():
            return await self._client.request(
                method,
                f"/gateway/{target_url}",
                headers=request_headers,
                content=content if content else None,
                timeout=PROXY_TIMEOUT_SECONDS,
            )


@attr.s(auto_attribs=True, frozen=True)
class AmbiguousAccount:
    service: str
    accounts: tuple[str, ...]


def _error_message(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    return error if isinstance(error, str) else None


def is_permission_denial(response: httpx.Response) -> bool:
    return response.status_code == 403 and _error_message(response) == PERMISSION_DENIED_MESSAGE


def ambiguous_account(response: httpx.Response) -> AmbiguousAccount | None:
    """The service and its stored accounts when the gateway refused for want of an account choice."""
    if response.status_code != 400:
        return None
    message = _error_message(response)
    if message is None:
        return None
    match = _AMBIGUOUS_ACCOUNT_RE.search(message)
    if match is None:
        return None
    return AmbiguousAccount(
        service=match.group("service"), accounts=tuple(re.findall(r"'([^']*)'", match.group("accounts")))
    )
