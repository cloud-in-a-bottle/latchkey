from server.core.proxying import extract_proxy_target
from server.core.proxying import forwardable_request_headers
from server.core.proxying import forwardable_response_headers


def test_extract_proxy_target_plain() -> None:
    assert (
        extract_proxy_target("/api/proxy/https://slack.com/api/conversations.list", "")
        == "https://slack.com/api/conversations.list"
    )


def test_extract_proxy_target_restores_collapsed_scheme() -> None:
    assert extract_proxy_target("/api/proxy/https:/slack.com/api/x", "") == "https://slack.com/api/x"
    assert extract_proxy_target("/api/proxy/http:/host:8080/x", "") == "http://host:8080/x"


def test_extract_proxy_target_appends_query() -> None:
    assert extract_proxy_target("/api/proxy/https://h/x", "a=1&b=2") == "https://h/x?a=1&b=2"


def test_extract_proxy_target_rejects_non_http() -> None:
    assert extract_proxy_target("/api/proxy/ftp://h/x", "") is None
    assert extract_proxy_target("/api/proxy/slack.com/api", "") is None
    assert extract_proxy_target("/other/https://h/x", "") is None


def test_request_headers_strip_credentials_and_router_headers() -> None:
    headers = [
        ("Authorization", "Bearer app-token"),
        ("Cookie", "owner-session"),
        ("X-OpenHost-Consumer-Id", "abc"),
        ("X-OpenHost-Permissions", "[]"),
        ("Host", "latchkey.zone"),
        ("Content-Length", "12"),
        ("Content-Type", "application/json"),
        ("X-Custom", "kept"),
    ]
    assert forwardable_request_headers(headers) == [
        ("Content-Type", "application/json"),
        ("X-Custom", "kept"),
    ]


def test_response_headers_strip_framing() -> None:
    headers = [
        ("Content-Length", "5"),
        ("Content-Encoding", "gzip"),
        ("Transfer-Encoding", "chunked"),
        ("Content-Type", "application/json"),
        ("X-Rate-Limit", "99"),
    ]
    assert forwardable_response_headers(headers) == {
        "Content-Type": "application/json",
        "X-Rate-Limit": "99",
    }
