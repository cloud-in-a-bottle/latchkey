import json
from typing import Any

from litestar import Request
from litestar import get
from litestar.response import Template

from server.core.grants import ScopeGrant
from server.core.grants import parse_grant_payload


@get("/")
async def console() -> Template:
    return Template("console.html")


@get("/connect/{service_name:str}")
async def connect_page(service_name: str) -> Template:
    return Template("connect.html", context={"service_name": service_name})


def _parse_grant_param(value: object) -> ScopeGrant | None:
    if not isinstance(value, str) or value == "":
        return None
    try:
        payload = json.loads(value)
    except ValueError:
        return None
    return parse_grant_payload(payload)


@get("/grant")
async def grant_page(request: Request[Any, Any, Any]) -> Template:
    params = request.query_params
    grant = _parse_grant_param(params.get("grant"))
    return Template(
        "grant.html",
        context={
            "consumer_id": str(params.get("consumer_id", "")),
            "consumer_name": str(params.get("consumer_name", "")),
            "return_to": str(params.get("return_to", "")),
            "grant": grant,
            "grant_payload": grant.as_payload() if grant is not None else None,
            "schemas_pretty": json.dumps(dict(grant.as_payload()).get("schemas"), indent=2)
            if grant is not None and grant.schemas
            else None,
        },
    )
