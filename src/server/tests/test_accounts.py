import httpx
import pytest

from server.core.accounts import AccountCredentials
from server.core.accounts import ServiceAccounts
from server.core.accounts import parse_auth_list
from server.core.accounts import parse_credentials_map
from server.core.gateway_client import AmbiguousAccount
from server.core.gateway_client import ambiguous_account


def test_parse_auth_list_groups_accounts_per_service() -> None:
    payload = {
        "slack": {
            "bob@example.com": {"credentialType": "SlackCredentials", "credentialStatus": "valid"},
            "eve@example.com": {"credentialType": "SlackCredentials", "credentialStatus": "invalid"},
        },
        # The empty key is latchkey's unnamed default account.
        "echosvc": {"": {"credentialType": "RawCurlCredentials", "credentialStatus": "unknown"}},
    }
    assert parse_auth_list(payload) == (
        ServiceAccounts(
            service="slack",
            accounts=(
                AccountCredentials("bob@example.com", "SlackCredentials", "valid"),
                AccountCredentials("eve@example.com", "SlackCredentials", "invalid"),
            ),
        ),
        ServiceAccounts(
            service="echosvc",
            accounts=(AccountCredentials("", "RawCurlCredentials", "unknown"),),
        ),
    )


def test_parse_credentials_map_of_unconnected_service_is_empty() -> None:
    assert parse_credentials_map({}) == ()


def test_parse_credentials_map_rejects_unexpected_shapes() -> None:
    # The pre-3.0 shape (one status per service) must not be read as an account map.
    with pytest.raises(ValueError):
        parse_credentials_map({"credentialType": "SlackCredentials", "credentialStatus": "valid"})
    with pytest.raises(ValueError):
        parse_auth_list(["slack"])


def _gateway_error(status_code: int, message: str) -> httpx.Response:
    return httpx.Response(status_code, json={"error": message})


def test_ambiguous_account_parsed_from_gateway_error() -> None:
    response = _gateway_error(
        400,
        "Multiple accounts are stored for service 'slack': 'bob@example.com', 'eve@example.com'. "
        "Specify which one to use with --account.",
    )
    assert ambiguous_account(response) == AmbiguousAccount(
        service="slack", accounts=("bob@example.com", "eve@example.com")
    )


def test_other_gateway_errors_are_not_ambiguous_accounts() -> None:
    assert ambiguous_account(_gateway_error(400, "Error: No service matches URL: https://nope.test/")) is None
    assert ambiguous_account(_gateway_error(200, "")) is None
    assert ambiguous_account(httpx.Response(400, text="not json")) is None
