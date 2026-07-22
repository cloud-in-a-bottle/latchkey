import shlex
from typing import Any

import attr

from server.grants import GrantPayload


def _validate_non_empty_str(_instance: Any, attribute: Any, value: Any) -> None:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"'{attribute.name}' must be a non-empty string")


def _validate_optional_non_empty_str(_instance: Any, attribute: Any, value: Any) -> None:
    if value is not None and (not isinstance(value, str) or value == ""):
        raise ValueError(f"'{attribute.name}' must be a non-empty string when present")


# ─── requests ───


@attr.s(auto_attribs=True, frozen=True)
class AuthSetRequest:
    service_name: str = attr.ib(validator=_validate_non_empty_str)
    curl_args: str = attr.ib()

    @curl_args.validator
    def _validate_curl_args(self, _attribute: Any, value: object) -> None:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("'curl_args' must be a non-empty string, e.g. -H \"Authorization: Bearer ...\"")
        try:
            shlex.split(value)
        except ValueError as e:
            raise ValueError(f"could not parse curl arguments: {e}") from e


@attr.s(auto_attribs=True, frozen=True)
class AuthClearRequest:
    service_name: str | None = attr.ib(default=None, validator=_validate_optional_non_empty_str)


@attr.s(auto_attribs=True, frozen=True)
class ServicesRegisterRequest:
    service_name: str = attr.ib(validator=_validate_non_empty_str)
    base_api_url: str = attr.ib(validator=_validate_non_empty_str)
    service_family: str | None = attr.ib(default=None, validator=_validate_optional_non_empty_str)


@attr.s(auto_attribs=True, frozen=True)
class BrowserLoginStartRequest:
    service_name: str = attr.ib(validator=_validate_non_empty_str)


@attr.s(auto_attribs=True, frozen=True)
class ApproveGrantRequest:
    consumer_app_id: str = attr.ib(validator=_validate_non_empty_str)
    grant: GrantPayload


@attr.s(auto_attribs=True, frozen=True)
class RequestGrantRequest:
    grant: GrantPayload
    return_to: str | None = attr.ib(default=None, validator=_validate_optional_non_empty_str)


# ─── responses ───


@attr.s(auto_attribs=True, frozen=True)
class OkBody:
    ok: bool = True


@attr.s(auto_attribs=True, frozen=True)
class ErrorBody:
    error: str
    message: str


@attr.s(auto_attribs=True, frozen=True)
class RequiredGrant:
    grant: GrantPayload
    scope: str = "app"


@attr.s(auto_attribs=True, frozen=True)
class PermissionRequiredBody:
    message: str
    grant_url: str
    required_grant: RequiredGrant | None = None
    error: str = "permission_required"


@attr.s(auto_attribs=True, frozen=True)
class GrantUrlBody:
    grant_url: str
    required_grant: RequiredGrant


@attr.s(auto_attribs=True, frozen=True)
class ServicesListBody:
    services: list[str]


@attr.s(auto_attribs=True, frozen=True)
class BrowserLoginStatusBody:
    state: str
    service: str | None = None
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class StatusBody:
    gateway_healthy: bool
    # Latchkey RPC passthrough: {service: {credentialType, credentialStatus}} and service names.
    auth: dict[str, Any] | None = None
    services: list[str] | None = None
    browser_login: BrowserLoginStatusBody | None = None
