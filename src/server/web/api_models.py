import shlex
from typing import Any

import attr

from server.core.accounts import ServiceAccounts
from server.core.grants import GrantPayload


def _validate_non_empty_str(_instance: Any, attribute: Any, value: Any) -> None:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"'{attribute.name}' must be a non-empty string")


def _validate_optional_non_empty_str(_instance: Any, attribute: Any, value: Any) -> None:
    if value is not None and (not isinstance(value, str) or value == ""):
        raise ValueError(f"'{attribute.name}' must be a non-empty string when present")


def _validate_optional_account(_instance: Any, attribute: Any, value: Any) -> None:
    """Accounts may be the empty string: that is latchkey's default account, not a missing value."""
    if value is not None and not isinstance(value, str):
        raise ValueError(f"'{attribute.name}' must be a string when present")


# ─── requests ───


@attr.s(auto_attribs=True, frozen=True)
class AuthSetRequest:
    service_name: str = attr.ib(validator=_validate_non_empty_str)
    curl_args: str = attr.ib()
    account: str | None = attr.ib(default=None, validator=_validate_optional_account)

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
    """Clears one account, or — with `all_accounts` — every account of a service plus its setup.

    Without a service name it clears everything latchkey stores.
    """

    service_name: str | None = attr.ib(default=None, validator=_validate_optional_non_empty_str)
    account: str | None = attr.ib(default=None, validator=_validate_optional_account)
    all_accounts: bool = attr.ib(default=False)

    @all_accounts.validator
    def _validate_all_accounts(self, _attribute: Any, value: object) -> None:
        if not isinstance(value, bool):
            raise ValueError("'all_accounts' must be a boolean")
        if value and self.service_name is None:
            raise ValueError("'all_accounts' requires a service name")
        if value and self.account is not None:
            raise ValueError("'all_accounts' cannot be combined with an account")


@attr.s(auto_attribs=True, frozen=True)
class ServicesRegisterRequest:
    service_name: str = attr.ib(validator=_validate_non_empty_str)
    base_api_url: str = attr.ib(validator=_validate_non_empty_str)
    service_family: str | None = attr.ib(default=None, validator=_validate_optional_non_empty_str)


@attr.s(auto_attribs=True, frozen=True)
class BrowserLoginStartRequest:
    service_name: str = attr.ib(validator=_validate_non_empty_str)
    # Reuse the one-time setup stored with this account's credentials (for services whose login
    # needs an OAuth client); the account logged in as is whatever the owner signs in with.
    reuse_account: str | None = attr.ib(default=None, validator=_validate_optional_account)


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
class AccountRequiredBody:
    """The owner has several accounts for the service and the request named none of them."""

    service: str
    accounts: list[str]
    message: str
    error: str = "account_required"


@attr.s(auto_attribs=True, frozen=True)
class BrowserLoginStatusBody:
    state: str
    service: str | None = None
    error: str | None = None
    viewer_connected: bool = False
    # Set once the login succeeded: the account latchkey stored the credentials under
    # ("" is its default account, used when the service can't report one).
    logged_in_account: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class StatusBody:
    gateway_healthy: bool
    connected: list[ServiceAccounts] | None = None
    services: list[str] | None = None
    browser_login: BrowserLoginStatusBody | None = None
