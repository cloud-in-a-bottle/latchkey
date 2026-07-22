import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import attr
from loguru import logger

from server.core.config import AppConfig
from server.core.latchkey_runtime import LatchkeyRuntime

# Pseudo-scope handled by this app itself (not passed to detent): metadata access.
META_SCOPE = "latchkey-meta"
PERMISSION_SERVICES_READ = "services-read"

_CONSUMER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@attr.s(auto_attribs=True, frozen=True)
class GrantPayload:
    """The wire shape of one grant, as declared in consumer manifests and stored by the router.

    `schemas` optionally defines custom detent request schemas for services detent's builtin schemas
    don't cover, e.g. self-hosted or runtime-registered services. attrs validators are the single
    source of validation: litestar runs them when binding request bodies, and header parsing goes
    through them via `parse_grant_payload`.
    """

    scope: str = attr.ib()
    permissions: list[str] = attr.ib()
    schemas: dict[str, dict[str, Any]] | None = attr.ib(default=None)

    @scope.validator
    def _validate_scope(self, _attribute: Any, value: object) -> None:
        if not isinstance(value, str) or value == "":
            raise ValueError("'scope' must be a non-empty string")

    @permissions.validator
    def _validate_permissions(self, _attribute: Any, value: object) -> None:
        if not isinstance(value, list) or not value or not all(isinstance(p, str) and p != "" for p in value):
            raise ValueError("'permissions' must be a non-empty list of non-empty strings")

    @schemas.validator
    def _validate_schemas(self, _attribute: Any, value: object) -> None:
        if value is None:
            return
        if not isinstance(value, dict) or not all(
            isinstance(name, str) and name != "" and isinstance(schema, dict) for name, schema in value.items()
        ):
            raise ValueError("'schemas' must map non-empty schema names to schema objects")

    def to_scope_grant(self) -> "ScopeGrant":
        schemas = self.schemas or {}
        return ScopeGrant(
            scope=self.scope,
            permissions=tuple(self.permissions),
            schemas=tuple((name, json.dumps(schema, sort_keys=True)) for name, schema in schemas.items()),
        )


@attr.s(auto_attribs=True, frozen=True)
class ScopeGrant:
    """Internal, hashable form of a grant: permissions as a tuple, schemas serialized per-name."""

    scope: str
    permissions: tuple[str, ...]
    schemas: tuple[tuple[str, str], ...] = ()

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"scope": self.scope, "permissions": list(self.permissions)}
        if self.schemas:
            payload["schemas"] = {name: json.loads(schema) for name, schema in self.schemas}
        return payload

    def to_payload_model(self) -> GrantPayload:
        return GrantPayload(
            scope=self.scope,
            permissions=list(self.permissions),
            schemas={name: json.loads(schema) for name, schema in self.schemas} if self.schemas else None,
        )


def parse_grant_payload(payload: object) -> ScopeGrant | None:
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        return None
    try:
        grant = GrantPayload(**payload)
    except (TypeError, ValueError):
        return None
    return grant.to_scope_grant()


def parse_permissions_header(header_value: str | None) -> tuple[ScopeGrant, ...]:
    """Parse the router-injected X-OpenHost-Permissions header into the grants this service understands.

    Entries whose payload doesn't match the GrantPayload shape are ignored (they may belong to other
    versions of this spec); malformed JSON fails loudly.
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


@attr.s(auto_attribs=True, frozen=True)
class ConsumerContext:
    """The calling app's identity and granted permissions, as attested by the router."""

    app_id: str
    app_name: str
    grants: tuple[ScopeGrant, ...]


def build_grant_url(
    own_url: str,
    consumer: ConsumerContext,
    grant: ScopeGrant | None = None,
    return_to: str | None = None,
) -> str:
    """URL of the owner-facing consent page, pre-filled with the consumer and requested grant."""
    params: dict[str, str] = {"consumer_id": consumer.app_id, "consumer_name": consumer.app_name}
    if grant is not None:
        params["grant"] = json.dumps(grant.as_payload())
    if return_to is not None:
        params["return_to"] = return_to
    return f"{own_url}/grant?{urlencode(params)}"


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
