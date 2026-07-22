import json

import pytest

from server.grants import ScopeGrant
from server.grants import has_meta_permission
from server.grants import parse_grant_payload
from server.grants import parse_permissions_header
from server.grants import permissions_config_json


def test_parse_permissions_header_empty() -> None:
    assert parse_permissions_header(None) == ()
    assert parse_permissions_header("") == ()
    assert parse_permissions_header("[]") == ()


def test_parse_permissions_header_valid_and_ignored() -> None:
    header = json.dumps(
        [
            {"grant": {"scope": "slack-api", "permissions": ["slack-read-all"]}, "scope": "global"},
            {"grant": "some-other-services-string-grant", "scope": "global"},
            {"grant": {"key": "DB_URL"}, "scope": "app"},
            {"grant": {"scope": "", "permissions": ["x"]}, "scope": "app"},
            {"grant": {"scope": "x", "permissions": []}, "scope": "app"},
        ]
    )
    grants = parse_permissions_header(header)
    assert grants == (ScopeGrant(scope="slack-api", permissions=("slack-read-all",)),)


def test_parse_permissions_header_malformed_json_fails() -> None:
    with pytest.raises(ValueError):
        parse_permissions_header("not json")
    with pytest.raises(ValueError):
        parse_permissions_header('{"not": "a list"}')


def test_parse_grant_payload_with_schemas() -> None:
    grant = parse_grant_payload(
        {
            "scope": "my-scope",
            "permissions": ["my-perm"],
            "schemas": {"my-scope": {"properties": {"domain": {"const": "example.com"}}}},
        }
    )
    assert grant is not None
    assert grant.as_payload() == {
        "scope": "my-scope",
        "permissions": ["my-perm"],
        "schemas": {"my-scope": {"properties": {"domain": {"const": "example.com"}}}},
    }


def test_parse_grant_payload_rejects_bad_schemas() -> None:
    assert parse_grant_payload({"scope": "s", "permissions": ["p"], "schemas": {"": {}}}) is None
    assert parse_grant_payload({"scope": "s", "permissions": ["p"], "schemas": {"n": "not a dict"}}) is None
    assert parse_grant_payload({"scope": "s", "permissions": ["p"], "schemas": []}) is None


def test_meta_permission() -> None:
    grants = parse_permissions_header(
        json.dumps([{"grant": {"scope": "latchkey-meta", "permissions": ["services-read"]}, "scope": "global"}])
    )
    assert has_meta_permission(grants, "services-read")
    assert not has_meta_permission(grants, "other")


def test_permissions_config_empty_denies_all() -> None:
    assert json.loads(permissions_config_json(())) == {"rules": []}


def test_permissions_config_merges_scopes_and_excludes_meta() -> None:
    grants = (
        ScopeGrant(scope="slack-api", permissions=("slack-read-all",)),
        ScopeGrant(scope="slack-api", permissions=("slack-read-all", "slack-write-all")),
        ScopeGrant(scope="github-rest-api", permissions=("github-read-all",)),
        ScopeGrant(scope="latchkey-meta", permissions=("services-read",)),
    )
    config = json.loads(permissions_config_json(grants))
    assert config == {
        "rules": [
            {"slack-api": ["slack-read-all", "slack-write-all"]},
            {"github-rest-api": ["github-read-all"]},
        ]
    }


def test_permissions_config_includes_custom_schemas() -> None:
    schema = {"properties": {"domain": {"const": "example.com"}}, "required": ["domain"]}
    grant = parse_grant_payload(
        {"scope": "custom-scope", "permissions": ["custom-get"], "schemas": {"custom-scope": schema}}
    )
    assert grant is not None
    config = json.loads(permissions_config_json((grant,)))
    assert config == {
        "rules": [{"custom-scope": ["custom-get"]}],
        "schemas": {"custom-scope": schema},
    }
