from typing import Any

import httpx

from server.config import AppConfig

PASSWORD_HEADER = "X-Latchkey-Gateway-Password"
PERMISSIONS_OVERRIDE_HEADER = "X-Latchkey-Gateway-Permissions-Override"

# The gateway signals a detent policy denial with this exact JSON error body
# (upstream 403s arrive with the upstream's own body instead).
PERMISSION_DENIED_MESSAGE = "Error: Request not permitted by the user."

PROXY_TIMEOUT_SECONDS = 120.0
RPC_TIMEOUT_SECONDS = 60.0
BROWSER_LOGIN_TIMEOUT_SECONDS = 600.0


class GatewayRpcError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"gateway RPC failed ({status_code}): {message}")
        self.status_code = status_code
        self.message = message


class GatewayClient:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(base_url=config.gateway_url)

    async def close(self) -> None:
        await self._client.aclose()

    async def is_healthy(self) -> bool:
        try:
            response = await self._client.get("/", headers=self._base_headers(), timeout=2.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def _base_headers(self) -> dict[str, str]:
        return {PASSWORD_HEADER: self._config.gateway_password}

    async def rpc(
        self, command: str, params: dict[str, Any] | None = None, timeout: float = RPC_TIMEOUT_SECONDS
    ) -> Any:
        body: dict[str, Any] = {"command": command}
        if params is not None:
            body["params"] = params
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
    ) -> httpx.Response:
        """Forward a request through /gateway/<target_url> with a per-consumer permissions override."""
        request_headers = list(headers)
        request_headers.append((PASSWORD_HEADER, self._config.gateway_password))
        request_headers.append((PERMISSIONS_OVERRIDE_HEADER, permissions_jwt))
        return await self._client.request(
            method,
            f"/gateway/{target_url}",
            headers=request_headers,
            content=content if content else None,
            timeout=PROXY_TIMEOUT_SECONDS,
        )


def is_permission_denial(response: httpx.Response) -> bool:
    if response.status_code != 403:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error") == PERMISSION_DENIED_MESSAGE
