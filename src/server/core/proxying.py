import re

PROXY_PATH_PREFIX = "/api/proxy/"

# Request headers never forwarded upstream. The router's service proxy authenticates callers
# for us; Authorization and Cookie from the original request must not leak to third parties
# (browser-originated service calls carry the owner's zone session cookie).
_STRIPPED_REQUEST_HEADERS = {
    "host",
    "authorization",
    "cookie",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "expect",
}

# Hop-by-hop headers, plus content-length/-encoding since the proxied body is re-framed.
_STRIPPED_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "trailers",
}


def extract_proxy_target(raw_path: str, query: str) -> str | None:
    """Extract the absolute target URL from a raw /api/proxy/<url> request path.

    Returns None when the path isn't a proxy path or the target isn't an absolute http(s) URL.
    """
    if not raw_path.startswith(PROXY_PATH_PREFIX):
        return None
    target = raw_path[len(PROXY_PATH_PREFIX) :]
    # Proxies (including the router) may collapse "//" in paths; restore the scheme separator.
    if re.match(r"^https?:/[^/]", target):
        target = target.replace(":/", "://", 1)
    if query:
        target = f"{target}?{query}"
    if not target.startswith(("http://", "https://")):
        return None
    return target


def forwardable_request_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in headers
        if name.lower() not in _STRIPPED_REQUEST_HEADERS and not name.lower().startswith("x-openhost-")
    ]


def forwardable_response_headers(headers: list[tuple[str, str]]) -> dict[str, str]:
    return {name: value for name, value in headers if name.lower() not in _STRIPPED_RESPONSE_HEADERS}
