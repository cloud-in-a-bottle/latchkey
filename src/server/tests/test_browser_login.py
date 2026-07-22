import asyncio
from pathlib import Path
from typing import Any

import pytest

from server.core.browser_login import BrowserLoginManager
from server.core.browser_login import LoginAlreadyRunningError
from server.core.browser_login import LoginState
from server.core.config import AppConfig
from server.core.gateway_client import GatewayClient
from server.core.gateway_client import GatewayRpcError


def _config() -> AppConfig:
    return AppConfig(
        app_name="latchkey",
        app_data_dir=Path("/tmp"),
        router_url="http://router",
        app_token="token",
        zone_domain="zone.test",
        gateway_host="127.0.0.1",
        gateway_port=1,
        gateway_password="pw",
        vnc_host="127.0.0.1",
        vnc_port=1,
    )


class FakeGateway(GatewayClient):
    def __init__(self, fail_on: str | None = None) -> None:
        super().__init__(_config())
        self.fail_on = fail_on
        self.calls: list[str] = []

    async def rpc(self, command: str, params: dict[str, Any] | None = None, timeout: float = 0) -> Any:
        self.calls.append(command)
        if command == self.fail_on:
            raise GatewayRpcError(400, f"{command} failed")
        return None


def test_login_runs_prepare_then_browser() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        manager = BrowserLoginManager(gateway)
        job = manager.start("google-gmail")
        assert job.state == LoginState.PREPARING
        await manager.wait_until_done()
        assert gateway.calls == ["auth browser-prepare", "auth browser"]
        assert manager.current is not None and manager.current.state == LoginState.SUCCEEDED

    asyncio.run(scenario())


def test_prepare_failure_skips_login() -> None:
    async def scenario() -> None:
        gateway = FakeGateway(fail_on="auth browser-prepare")
        manager = BrowserLoginManager(gateway)
        manager.start("google-gmail")
        await manager.wait_until_done()
        assert gateway.calls == ["auth browser-prepare"]
        current = manager.current
        assert current is not None and current.state == LoginState.FAILED
        assert current.error == "auth browser-prepare failed"

    asyncio.run(scenario())


def test_login_failure_reported() -> None:
    async def scenario() -> None:
        gateway = FakeGateway(fail_on="auth browser")
        manager = BrowserLoginManager(gateway)
        manager.start("slack")
        await manager.wait_until_done()
        current = manager.current
        assert current is not None and current.state == LoginState.FAILED

    asyncio.run(scenario())


def test_second_start_rejected_while_active() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        manager = BrowserLoginManager(gateway)
        manager.start("slack")
        with pytest.raises(LoginAlreadyRunningError):
            manager.start("github")
        await manager.wait_until_done()
        # After completion a new login may start.
        manager.start("github")
        await manager.wait_until_done()
        assert gateway.calls.count("auth browser") == 2

    asyncio.run(scenario())
