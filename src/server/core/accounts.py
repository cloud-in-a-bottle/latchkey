from typing import Any

import attr

# Latchkey stores credentials per service *and* per account, where an account is whatever identifies
# the third-party login (usually an e-mail). The empty string is its unnamed default account: what
# credentials stored before any account was known — or for services that can't report one — use.
DEFAULT_ACCOUNT = ""


@attr.s(auto_attribs=True, frozen=True)
class AccountCredentials:
    """One stored credential: `credential_status` is latchkey's missing/valid/invalid/unknown."""

    account: str
    credential_type: str
    credential_status: str


@attr.s(auto_attribs=True, frozen=True)
class ServiceAccounts:
    service: str
    accounts: tuple[AccountCredentials, ...]


def parse_credentials_map(payload: object) -> tuple[AccountCredentials, ...]:
    """Parse latchkey's account-keyed credentials object (`services info` -> "credentials")."""
    if not isinstance(payload, dict):
        raise ValueError(f"expected an account-keyed credentials object, got {type(payload).__name__}")
    entries: list[AccountCredentials] = []
    for account, entry in payload.items():
        if not isinstance(account, str) or not isinstance(entry, dict):
            raise ValueError(f"malformed credentials entry for account {account!r}")
        entries.append(
            AccountCredentials(
                account=account,
                credential_type=_string_field(entry, "credentialType"),
                credential_status=_string_field(entry, "credentialStatus"),
            )
        )
    return tuple(entries)


def parse_auth_list(payload: object) -> tuple[ServiceAccounts, ...]:
    """Parse latchkey's `auth list` result: {service: {account: {credentialType, credentialStatus}}}."""
    if not isinstance(payload, dict):
        raise ValueError(f"expected a service-keyed auth list, got {type(payload).__name__}")
    listing: list[ServiceAccounts] = []
    for service, credentials in payload.items():
        if not isinstance(service, str):
            raise ValueError(f"malformed service name in auth list: {service!r}")
        listing.append(ServiceAccounts(service=service, accounts=parse_credentials_map(credentials)))
    return tuple(listing)


def _string_field(entry: dict[str, Any], name: str) -> str:
    value = entry.get(name)
    if not isinstance(value, str):
        raise ValueError(f"missing or non-string {name!r} in credentials entry")
    return value
