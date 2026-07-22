import httpx

from server.core.config import AppConfig
from server.core.grants import ScopeGrant

SERVICE_URL = "github.com/imbue-openhost/openhost-latchkey/services/latchkey"


class GrantCreationError(Exception):
    pass


async def grant_app_scoped(config: AppConfig, consumer_app_id: str, grant: ScopeGrant) -> None:
    """Store an app-scoped grant in the router (after the owner approved it on the consent page)."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{config.router_url}/api/permissions/v2/grant_app_scoped",
            headers={"Authorization": f"Bearer {config.app_token}"},
            json={
                "consumer_app_id": consumer_app_id,
                "service_url": SERVICE_URL,
                "grant": grant.as_payload(),
            },
            timeout=10.0,
        )
    if response.status_code != 200:
        raise GrantCreationError(f"router returned {response.status_code}: {response.text[:300]}")
