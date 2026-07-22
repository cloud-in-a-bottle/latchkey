import json

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


def _parse_grant_param(value: str) -> ScopeGrant | None:
    if value == "":
        return None
    try:
        payload = json.loads(value)
    except ValueError:
        return None
    return parse_grant_payload(payload)


@get("/grant")
async def grant_page(
    consumer_id: str = "",
    consumer_name: str = "",
    grant: str = "",
    return_to: str = "",
) -> Template:
    parsed_grant = _parse_grant_param(grant)
    return Template(
        "grant.html",
        context={
            "consumer_id": consumer_id,
            "consumer_name": consumer_name,
            "return_to": return_to,
            "grant": parsed_grant,
            "grant_payload": parsed_grant.as_payload() if parsed_grant is not None else None,
            "schemas_pretty": json.dumps(dict(parsed_grant.as_payload()).get("schemas"), indent=2)
            if parsed_grant is not None and parsed_grant.schemas
            else None,
        },
    )
