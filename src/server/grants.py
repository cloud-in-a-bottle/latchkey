import asyncio
import json
import re
from pathlib import Path

import attr
from loguru import logger

from server.config import AppConfig
from server.latchkey_runtime import LatchkeyRuntime

# Pseudo-scope handled by this app itself (not passed to detent): metadata access.
META_SCOPE = "latchkey-meta"
PERMISSION_SERVICES_READ = "services-read"

_CONSUMER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@attr.s(auto_attribs=True, frozen=True)
class ScopeGrant:
    """One granted payload: a detent scope schema name plus permission schema names allowed within it.

    `schemas` optionally defines custom detent request schemas (JSON, serialized per-name) for services
    that detent's builtin schemas don't cover, e.g. self-hosted or runtime-registered services.
    """

    scope: str
    permissions: tuple[str, ...]
    schemas: tuple[tuple[str, str], ...] = ()

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"scope": self.scope, "permissions": list(self.permissions)}
        if self.schemas:
            payload["schemas"] = {name: json.loads(schema) for name, schema in self.schemas}
        return payload


def _parse_schemas(raw: object) -> tuple[tuple[str, str], ...] | None:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        return None
    schemas: list[tuple[str, str]] = []
    for name, schema in raw.items():
        if not isinstance(name, str) or name == "" or not isinstance(schema, dict):
            return None
        schemas.append((name, json.dumps(schema, sort_keys=True)))
    return tuple(schemas)


def parse_grant_payload(payload: object) -> ScopeGrant | None:
    if not isinstance(payload, dict):
        return None
    scope = payload.get("scope")
    permissions = payload.get("permissions")
    schemas = _parse_schemas(payload.get("schemas"))
    if (
        isinstance(scope, str)
        and scope != ""
        and isinstance(permissions, list)
        and permissions != []
        and all(isinstance(p, str) and p != "" for p in permissions)
        and schemas is not None
    ):
        return ScopeGrant(scope=scope, permissions=tuple(permissions), schemas=schemas)
    return None


def parse_permissions_header(header_value: str | None) -> tuple[ScopeGrant, ...]:
    """Parse the router-injected X-OpenHost-Permissions header into the grants this service understands.

    Entries whose payload doesn't match {"scope": str, "permissions": [str, ...], "schemas"?: {...}} are
    ignored (they may belong to other versions of this spec); malformed JSON fails loudly.
    """
    if header_value is None or header_value.strip() == "":
        return ()
    entries = json.loads(header_value)
    if not isinstance(entries, list):
        raise ValueError("X-OpenHost-Permissions is not a JSON array")
    grants: list[ScopeGrant] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        grant = parse_grant_payload(entry.get("grant"))
        if grant is not None:
            grants.append(grant)
        else:
            logger.warning("ignoring unrecognized grant payload: {}", entry.get("grant"))
    return tuple(grants)


def has_meta_permission(grants: tuple[ScopeGrant, ...], permission: str) -> bool:
    return any(g.scope == META_SCOPE and permission in g.permissions for g in grants)


def detent_grants(grants: tuple[ScopeGrant, ...]) -> tuple[ScopeGrant, ...]:
    return tuple(g for g in grants if g.scope != META_SCOPE)


def permissions_config_json(grants: tuple[ScopeGrant, ...]) -> str:
    """Build a latchkey/detent permissions.json from the non-meta grants.

    Scopes are merged (union of permissions, order preserved); custom schemas from all grants are merged
    into the config's "schemas" section. With no grants the config is {"rules": []}, which denies every
    request.
    """
    merged: dict[str, list[str]] = {}
    schemas: dict[str, object] = {}
    for grant in detent_grants(grants):
        permissions = merged.setdefault(grant.scope, [])
        for permission in grant.permissions:
            if permission not in permissions:
                permissions.append(permission)
        for name, schema_json in grant.schemas:
            schema = json.loads(schema_json)
            if name in schemas and schemas[name] != schema:
                logger.warning("conflicting custom schema definitions for {!r}; keeping the first", name)
            else:
                schemas.setdefault(name, schema)
    rules = [{scope: permissions} for scope, permissions in merged.items()]
    config: dict[str, object] = {"rules": rules}
    if schemas:
        config["schemas"] = schemas
    return json.dumps(config, indent=2)


class ConsumerPermissionFiles:
    """Per-consumer permissions.json files plus the JWTs that point the gateway at them.

    The JWT only embeds the file path, so it is minted once per consumer (per boot) and the
    file content is rewritten whenever the consumer's granted set changes.
    """

    def __init__(self, config: AppConfig, runtime: LatchkeyRuntime) -> None:
        self._config = config
        self._runtime = runtime
        self._jwt_cache: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _path_for(self, consumer_app_id: str) -> Path:
        if not _CONSUMER_ID_RE.match(consumer_app_id):
            raise ValueError(f"invalid consumer app id: {consumer_app_id!r}")
        return self._config.consumer_permissions_dir / f"{consumer_app_id}.json"

    async def jwt_for(self, consumer_app_id: str, grants: tuple[ScopeGrant, ...]) -> str:
        path = self._path_for(consumer_app_id)
        content = permissions_config_json(grants)
        async with self._lock:
            if not path.exists() or path.read_text() != content:
                temp_path = path.with_suffix(".json.tmp")
                temp_path.write_text(content)
                temp_path.replace(path)
            jwt = self._jwt_cache.get(consumer_app_id)
            if jwt is None:
                jwt = await self._runtime.create_permissions_jwt(str(path))
                self._jwt_cache[consumer_app_id] = jwt
            return jwt
