import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from typing import Any

import pytest
from consumer_app import call_service
from consumer_app import write_consumer_app
from openhost_test_harness import OpenhostStack

SERVICE = "github.com/imbue-openhost/openhost-latchkey/services/latchkey"

# Hostname at which app containers reach the test host (same alias the router itself uses).
CONTAINER_HOST_ALIAS = "host.containers.internal"


@pytest.fixture(scope="session")
def stack() -> Iterator[OpenhostStack]:
    """Build the app's Dockerfile, run it under podman per openhost.toml, and front it with the
    real OpenHost router.

    - stack.url                    — through the router; requires owner auth
    - stack.owner_session          — a requests.Session authenticated as the zone owner
    - stack.playwright_login(page) — log a playwright page in as the owner for browser tests
    - stack.app_url                — direct to the container (control your own headers)
    """
    with OpenhostStack() as s:
        yield s


class EchoHandler(BaseHTTPRequestHandler):
    """Fake third-party API: echoes back the path and the Authorization header it received."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass

    def _respond(self) -> None:
        body = json.dumps(
            {
                "path": self.path,
                "method": self.command,
                "authorization": self.headers.get("Authorization"),
                "cookie": self.headers.get("Cookie"),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()


@pytest.fixture(scope="session")
def echo_server() -> Iterator[str]:
    """A fake third-party API on the host, reachable from app containers; yields its base URL."""
    with socket.socket() as probe:
        probe.bind(("", 0))
        port = probe.getsockname()[1]
    server = HTTPServer(("0.0.0.0", port), EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://{CONTAINER_HOST_ALIAS}:{port}"
    server.shutdown()


class DeployedConsumer:
    def __init__(self, stack: OpenhostStack, name: str, app_id: str, shortname: str) -> None:
        self.stack = stack
        self.name = name
        self.app_id = app_id
        self.shortname = shortname

    def call(self, path: str, method: str = "GET", payload: Any = None) -> tuple[int, Any]:
        return call_service(
            self.stack.owner_session, self.stack.url_for(self.name), self.shortname, path, method, payload
        )


def _deploy_consumer(stack: OpenhostStack, name: str) -> DeployedConsumer:
    app_dir = stack._data_dir / "custom-consumers" / name
    write_consumer_app(app_dir, name=name, service=SERVICE)
    app_id = stack.deploy_app(f"file://{app_dir}")
    return DeployedConsumer(stack=stack, name=name, app_id=app_id, shortname="lk")


@pytest.fixture(scope="session")
def consumer(stack: OpenhostStack) -> DeployedConsumer:
    """A consumer app of the latchkey service; grants are added per-test."""
    return _deploy_consumer(stack, "lk-consumer")


@pytest.fixture(scope="session")
def bare_consumer(stack: OpenhostStack) -> DeployedConsumer:
    """A consumer that never receives any grants."""
    return _deploy_consumer(stack, "lk-bare")
