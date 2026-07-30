"""A test consumer app like the harness's synthetic one, but allowing URL-shaped service paths.

The harness consumer restricts paths to [A-Za-z0-9_/-]*, which rejects this service's main path
shape (proxy/https://host/path?query). This one forwards the path verbatim.
"""

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlsplit

_SERVER = """
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/call-service":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        url = os.environ["OPENHOST_ROUTER_URL"] + "/api/services/v2/call/" + body["shortname"] + "/" + body["path"]
        request = urllib.request.Request(
            url,
            data=None if body.get("payload") is None else json.dumps(body["payload"]).encode(),
            method=body.get("method", "GET"),
            headers={
                "Authorization": "Bearer " + os.environ["OPENHOST_APP_TOKEN"],
                "Content-Type": "application/json",
                **body.get("headers", {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            self._json(502, {"error": "router unreachable", "detail": str(e)})
            return
        try:
            service_body = json.loads(raw)
        except ValueError:
            service_body = raw.decode("utf-8", "replace")
        self._json(200, {"service_status": status, "service_body": service_body})


if __name__ == "__main__":
    print("Test consumer listening on :5000", flush=True)
    HTTPServer(("0.0.0.0", 5000), Handler).serve_forever()
"""

_DOCKERFILE = """\
FROM python:3.12-alpine
COPY server.py /server.py
CMD ["python", "/server.py"]
"""

_MANIFEST = """\
[app]
name = "{name}"
version = "0.1.0"
description = "Test consumer for the latchkey service"
hidden = true

[runtime.container]
image = "Dockerfile"
port = 5000

[routing]
health_check = "/health"

[resources]
memory_mb = 64
cpu_cores = 0.1

[[services.v2.consumes]]
service = "{service}"
shortname = "{shortname}"
version = ">=0.1.0"
grants = []
"""


def write_consumer_app(target_dir: Path, name: str, service: str, shortname: str = "lk") -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "openhost.toml").write_text(_MANIFEST.format(name=name, service=service, shortname=shortname))
    (target_dir / "Dockerfile").write_text(_DOCKERFILE)
    (target_dir / "server.py").write_text(_SERVER)


def call_service(
    session: Any,
    consumer_url: str,
    shortname: str,
    path: str,
    method: str = "GET",
    payload: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    response = session.post(
        f"{consumer_url}/call-service",
        json={
            "shortname": shortname,
            "path": path,
            "method": method,
            "payload": payload,
            "headers": headers or {},
        },
        timeout=90,
    )
    assert response.status_code == 200, f"consumer /call-service failed: {response.status_code}: {response.text[:300]}"
    result = response.json()
    return result["service_status"], result["service_body"]


def parse_grant_url_query(grant_url: str) -> dict[str, str]:
    parsed = urlsplit(grant_url)
    return {k: v[0] for k, v in parse_qs(parsed.query).items()}


def grant_payload_from_url(grant_url: str) -> dict[str, Any]:
    params = parse_grant_url_query(grant_url)
    payload: dict[str, Any] = json.loads(params["grant"])
    return payload
