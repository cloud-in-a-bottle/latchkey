import base64
import socket
from urllib.parse import urlsplit

import httpx
from conftest import SERVICE
from conftest import DeployedConsumer
from consumer_app import grant_payload_from_url
from consumer_app import parse_grant_url_query
from openhost_test_harness import OpenhostStack
from playwright.sync_api import Page
from playwright.sync_api import expect

META_GRANT = {"scope": "latchkey-meta", "permissions": ["services-read"]}


def _echo_grant(echo_server: str, scope: str, path_prefix: str, method: str = "GET") -> dict[str, object]:
    """A grant allowing `method` requests to `path_prefix`* on the echo server, via custom schemas.

    The scope schema matches domain + path prefix (not just domain): detent picks the first rule
    whose scope matches, so domain-only scopes from different grants would shadow each other.
    """
    host = echo_server.removeprefix("http://").split(":")[0]
    return {
        "scope": scope,
        "permissions": [f"{scope}-allowed"],
        "schemas": {
            scope: {
                "properties": {"domain": {"const": host}, "path": {"pattern": f"^{path_prefix}"}},
                "required": ["domain", "path"],
            },
            f"{scope}-allowed": {"properties": {"method": {"const": method}}, "required": ["method"]},
        },
    }


def test_health_endpoint(stack: OpenhostStack) -> None:
    response = httpx.get(f"{stack.app_url}/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_console_renders_and_gateway_healthy(stack: OpenhostStack, page: Page) -> None:
    stack.playwright_login(page)
    page.goto(stack.url)
    expect(page.get_by_role("heading", name="Latchkey")).to_be_visible()
    # The console fetches /owner/api/status; a healthy gateway populates the available-services table.
    expect(page.locator("#available tbody tr").first).to_be_visible(timeout=15000)
    assert "slack" in page.locator("#available").inner_text()


def test_service_api_requires_consumer_headers(stack: OpenhostStack) -> None:
    response = httpx.get(f"{stack.app_url}/api/services")
    assert response.status_code == 400
    assert response.json()["error"] == "bad_request"


def test_proxy_without_grants_is_denied(stack: OpenhostStack, bare_consumer: DeployedConsumer) -> None:
    status, body = bare_consumer.call("proxy/https://example.com/anything")
    assert status == 403
    assert body["error"] == "permission_required"
    assert "/grant?" in body["grant_url"]
    params = parse_grant_url_query(body["grant_url"])
    assert params["consumer_name"] == bare_consumer.name


def test_services_listing_requires_meta_grant(stack: OpenhostStack, consumer: DeployedConsumer) -> None:
    status, body = consumer.call("services")
    assert status == 403
    assert body["error"] == "permission_required"
    assert grant_payload_from_url(body["grant_url"]) == META_GRANT

    stack.grant(consumer.app_id, SERVICE, META_GRANT)

    status, body = consumer.call("services")
    assert status == 200
    assert "slack" in body["services"]

    status, body = consumer.call("services/slack")
    assert status == 200
    assert body["credentialStatus"] == "missing"
    assert "browser" in body["authOptions"]

    status, body = consumer.call("services/no-such-service")
    assert status == 404


def test_proxy_injects_credentials_and_enforces_permissions(
    stack: OpenhostStack, consumer: DeployedConsumer, echo_server: str
) -> None:
    # Owner registers the echo server as a custom service and stores credentials for it.
    owner = stack.owner_session
    response = owner.post(
        f"{stack.url}/owner/api/services/register",
        json={"service_name": "echosvc", "base_api_url": f"{echo_server}/api/"},
        timeout=30,
    )
    assert response.status_code in (200, 201), response.text
    response = owner.post(
        f"{stack.url}/owner/api/auth/set",
        json={"service_name": "echosvc", "curl_args": '-H "Authorization: Bearer echo-sekrit"'},
        timeout=30,
    )
    assert response.status_code in (200, 201), response.text

    stack.grant(consumer.app_id, SERVICE, _echo_grant(echo_server, "echosvc-alpha", "/api/alpha"))

    # Allowed: GET under /api/alpha, with credentials injected (and no Cookie leaked).
    status, body = consumer.call(f"proxy/{echo_server}/api/alpha?x=1")
    assert status == 200, body
    assert body["authorization"] == "Bearer echo-sekrit"
    assert body["cookie"] is None
    assert body["path"] == "/api/alpha?x=1"

    # Denied: POST to the same path, and GET outside the granted prefix.
    status, body = consumer.call(f"proxy/{echo_server}/api/alpha", method="POST", payload={"a": 1})
    assert status == 403
    assert body["error"] == "permission_required"
    status, body = consumer.call(f"proxy/{echo_server}/api/other")
    assert status == 403


def test_app_scoped_grant_flow(stack: OpenhostStack, consumer: DeployedConsumer, echo_server: str) -> None:
    wanted = _echo_grant(echo_server, "echosvc-beta", "/api/beta")

    # Consumer asks for a grant; gets a consent URL for the owner.
    status, body = consumer.call(
        "grants/request",
        method="POST",
        payload={"grant": wanted, "return_to": "https://consumer.example/done"},
    )
    assert status == 200, body
    params = parse_grant_url_query(body["grant_url"])
    assert params["consumer_id"] == consumer.app_id
    assert params["return_to"] == "https://consumer.example/done"
    assert grant_payload_from_url(body["grant_url"]) == wanted

    # Owner opens the consent page (same path+query via the router) and approves.
    owner = stack.owner_session
    split = urlsplit(body["grant_url"])
    consent = owner.get(f"{stack.url}{split.path}?{split.query}", timeout=30)
    assert consent.status_code == 200
    assert consumer.name in consent.text

    response = owner.post(
        f"{stack.url}/owner/api/grants/approve",
        json={"consumer_app_id": consumer.app_id, "grant": wanted},
        timeout=30,
    )
    assert response.status_code in (200, 201), response.text

    # The consumer's calls under the newly granted prefix now succeed.
    status, body = consumer.call(f"proxy/{echo_server}/api/beta")
    assert status == 200, body
    assert body["authorization"] == "Bearer echo-sekrit"


def test_browser_login_page_and_status(stack: OpenhostStack, page: Page) -> None:
    owner = stack.owner_session
    response = owner.get(f"{stack.url}/owner/api/browser-login/status", timeout=30)
    assert response.status_code == 200
    assert response.json() == {"state": "idle"}

    # noVNC assets are served for the connect page's viewer iframe.
    response = owner.get(f"{stack.url}/novnc/vnc.html", timeout=30)
    assert response.status_code == 200

    stack.playwright_login(page)
    page.goto(f"{stack.url}/connect/slack")
    expect(page.get_by_role("heading", name="Connect slack")).to_be_visible()
    expect(page.locator("#browser-card")).to_be_visible(timeout=15000)


def test_vnc_websocket_bridge_reaches_x11vnc(stack: OpenhostStack) -> None:
    """A raw websocket handshake on /owner/vnc should yield x11vnc's RFB banner, proving the
    Xvfb + x11vnc stack is up and the bridge connects to it."""
    split = urlsplit(stack.app_url)
    assert split.hostname is not None and split.port is not None
    key = base64.b64encode(b"0123456789abcdef").decode()
    request = (
        f"GET /owner/vnc HTTP/1.1\r\nHost: {split.hostname}:{split.port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    with socket.create_connection((split.hostname, split.port), timeout=15) as conn:
        conn.sendall(request.encode())
        conn.settimeout(15)
        data = b""
        # Read the 101 response headers, then the first websocket frame (x11vnc's RFB greeting).
        while b"\r\n\r\n" not in data:
            data += conn.recv(4096)
        assert data.startswith(b"HTTP/1.1 101"), data[:100]
        frame = data.split(b"\r\n\r\n", 1)[1]
        while b"RFB " not in frame:
            chunk = conn.recv(4096)
            assert chunk != b"", "connection closed before RFB banner"
            frame += chunk
    assert b"RFB " in frame
