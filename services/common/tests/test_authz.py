"""ARN matching and the per-source access policy."""

from __future__ import annotations

import json

import pytest

from entcommon.authz import AccessPolicy, expand_principal, identities, matches

ACCT = "111122223333"
POLICY = AccessPolicy.from_json(
    json.dumps(
        {
            "sources": {
                "news": {
                    "tenant_id": "t-news",
                    "writers": [f"arn:aws:iam::{ACCT}:role/w"],
                    "readers": [f"arn:aws:iam::{ACCT}:role/r-*"],
                    "pii": "redact",
                },
                "sports": {"tenant_id": "t-sports", "writers": [f"arn:aws:iam::{ACCT}:user/bob"]},
                "weird": {"tenant_id": "t-news", "pii": "maybe"},
                "cn": {
                    "tenant_id": "t-cn",
                    "readers": [f"arn:aws-cn:iam::{ACCT}:role/cn-reader"],
                    "pii": "block",
                },
            },
            "admins": [f"arn:aws:iam::{ACCT}:role/admin"],
        }
    )
)


def session(role: str, partition: str = "aws") -> str:
    return f"arn:{partition}:sts::{ACCT}:assumed-role/{role}/sess-1"


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        (session("news-search"), [session("news-search"), f"arn:aws:iam::{ACCT}:role/news-search"]),
        (
            session("x", "aws-us-gov"),
            [session("x", "aws-us-gov"), f"arn:aws-us-gov:iam::{ACCT}:role/x"],
        ),
        (session("x", "aws-cn"), [session("x", "aws-cn"), f"arn:aws-cn:iam::{ACCT}:role/x"]),
        (f"arn:aws:iam::{ACCT}:user/bob", [f"arn:aws:iam::{ACCT}:user/bob"]),
        (f"arn:aws:iam::{ACCT}:role/w", [f"arn:aws:iam::{ACCT}:role/w"]),
        ("arn:aws:sts::123:assumed-role/x/s", ["arn:aws:sts::123:assumed-role/x/s"]),  # bad acct
        (f"arn:aws:sts::{ACCT}:assumed-role/x", [f"arn:aws:sts::{ACCT}:assumed-role/x"]),
        ("", [""]),
    ],
)
def test_identities(caller: str, expected: list[str]) -> None:
    assert identities(caller) == expected


@pytest.mark.parametrize(
    ("caller", "patterns", "expected"),
    [
        (session("w"), [f"arn:aws:iam::{ACCT}:role/w"], True),  # session matches its role
        (session("w"), [f"arn:aws:sts::{ACCT}:assumed-role/w/*"], True),  # or itself
        (session("w2"), [f"arn:aws:iam::{ACCT}:role/w"], False),  # no prefix match
        (session("r-search"), [f"arn:aws:iam::{ACCT}:role/r-*"], True),  # wildcard
        (session("R-search"), [f"arn:aws:iam::{ACCT}:role/r-*"], False),  # case-sensitive
        (session("x", "aws-cn"), [f"arn:aws-cn:iam::{ACCT}:role/x"], True),  # other partition
        (session("x", "aws-cn"), [f"arn:aws:iam::{ACCT}:role/x"], False),  # partition matters
        (session("x"), ["arn:aws:iam::*:role/x"], True),
        (f"arn:aws:iam::{ACCT}:user/bob", [f"arn:aws:iam::{ACCT}:user/*"], True),
        (f"arn:aws:iam::{ACCT}:user/bob", [], False),
        (session("x"), ["*"], True),
    ],
)
def test_matches(caller: str, patterns: list[str], expected: bool) -> None:
    assert matches(caller, patterns) is expected
    assert matches(caller, tuple(patterns)) is expected


@pytest.mark.parametrize(
    ("principal", "partition", "expected"),
    [
        ("role/deploy", "aws", f"arn:aws:iam::{ACCT}:role/deploy"),
        ("user/alice", "aws", f"arn:aws:iam::{ACCT}:user/alice"),
        ("role/team-*", "aws", f"arn:aws:iam::{ACCT}:role/team-*"),
        ("role/x", "aws-cn", f"arn:aws-cn:iam::{ACCT}:role/x"),
        ("arn:aws:iam::999999999999:role/y", "aws", "arn:aws:iam::999999999999:role/y"),
        ("arn:aws-us-gov:iam::1:user/z", "aws", "arn:aws-us-gov:iam::1:user/z"),
    ],
)
def test_expand_principal(principal: str, partition: str, expected: str) -> None:
    assert expand_principal(principal, ACCT, partition) == expected


@pytest.mark.parametrize("principal", ["group/x", "deploy", "", "role/"])
def test_expand_principal_rejects_other_shapes(principal: str) -> None:
    with pytest.raises(ValueError, match="principal"):
        expand_principal(principal, ACCT)


@pytest.mark.parametrize(
    ("caller", "source", "level"),
    [
        (session("w"), "news", "write"),
        (session("r-search"), "news", "read"),
        (session("admin"), "news", "write"),
        (session("admin"), "unconfigured", "write"),  # admins may do anything
        (session("w"), "sports", None),
        (f"arn:aws:iam::{ACCT}:user/bob", "sports", "write"),
        (session("r-search"), "sports", None),
        (session("other"), "news", None),
        (session("w"), "unconfigured", None),
        (session("cn-reader", "aws-cn"), "cn", "read"),
        (session("cn-reader"), "cn", None),
    ],
)
def test_levels(caller: str, source: str, level: str | None) -> None:
    assert POLICY.level(caller, source) == level
    assert POLICY.can_write(caller, source) is (level == "write")
    assert POLICY.can_read(caller, source) is (level is not None)  # writers read too


@pytest.mark.parametrize(
    ("caller", "tenant", "expected"),
    [
        (session("w"), "t-news", True),
        (session("r-search"), "t-news", True),
        (session("r-search"), "t-sports", False),
        (f"arn:aws:iam::{ACCT}:user/bob", "t-sports", True),
        (session("admin"), "t-anything", True),
        (session("other"), "t-news", False),
    ],
)
def test_can_search(caller: str, tenant: str, expected: bool) -> None:
    assert POLICY.can_search(caller, tenant) is expected


def test_tenants() -> None:
    assert POLICY.tenant_for("news") == "t-news"
    assert POLICY.tenant_for("unconfigured") is None


@pytest.mark.parametrize(
    ("source", "pii"),
    [
        ("news", "redact"),
        ("sports", "allow"),  # default
        ("cn", "block"),
        ("weird", "block"),  # unknown value: safest
        ("unconfigured", "allow"),
        (None, "allow"),
    ],
)
def test_pii(source: str | None, pii: str) -> None:
    assert POLICY.pii_for(source) == pii


@pytest.mark.parametrize("raw", [None, "", "{}", '{"sources": null, "admins": null}'])
def test_empty_policy_allows_nothing(raw: str | None) -> None:
    policy = AccessPolicy.from_json(raw)
    caller = session("admin")
    assert policy.level(caller, "news") is None
    assert not policy.can_write(caller, "news")
    assert not policy.can_read(caller, "news")
    assert not policy.can_search(caller, "t-news")
    assert not policy.is_admin(caller)
    assert policy.tenant_for("news") is None
    assert policy.pii_for("news") == "allow"


def test_missing_tenant_is_an_error() -> None:
    with pytest.raises(KeyError):
        AccessPolicy.from_json('{"sources": {"news": {"writers": []}}}')
