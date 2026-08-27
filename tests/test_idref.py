"""#N reference resolution."""

from __future__ import annotations

import pytest

from gmcli.cache import Cache
from gmcli.errors import UsageError
from gmcli.idref import looks_like_id, resolve, resolve_one


@pytest.fixture
def listed(isolated_dirs) -> Cache:
    cache = Cache("test@example.com")
    cache.set_listing("thread", [f"{i:016x}" for i in range(1, 11)])
    return cache


def test_single_reference(listed):
    assert resolve(["#3"], listed) == [f"{3:016x}"]


def test_reference_without_hash(listed):
    assert resolve(["3"], listed) == [f"{3:016x}"]


def test_range(listed):
    assert resolve(["#1-5"], listed) == [f"{i:016x}" for i in range(1, 6)]


def test_comma_list(listed):
    assert resolve(["#1,3,7"], listed) == [f"{i:016x}" for i in (1, 3, 7)]


def test_mixed_range_and_list(listed):
    assert resolve(["#2-4,9"], listed) == [f"{i:016x}" for i in (2, 3, 4, 9)]


def test_multiple_tokens(listed):
    assert resolve(["#1", "#2"], listed) == [f"{i:016x}" for i in (1, 2)]


def test_duplicates_collapse(listed):
    assert resolve(["#1,1", "#1"], listed) == [f"{1:016x}"]


def test_full_id_passes_through(listed):
    raw = "18f2a9c4b7d1e0aa"
    assert resolve([raw], listed) == [raw]


def test_full_id_works_without_a_listing(isolated_dirs):
    cache = Cache("nobody@example.com")
    assert resolve(["18f2a9c4b7d1e0aa"], cache) == ["18f2a9c4b7d1e0aa"]


def test_reference_without_listing_is_a_clear_error(isolated_dirs):
    cache = Cache("nobody@example.com")
    with pytest.raises(UsageError, match="no previous listing"):
        resolve(["#1"], cache)


def test_reference_past_the_end_is_rejected(listed):
    with pytest.raises(UsageError, match="past the end"):
        resolve(["#99"], listed)


def test_zero_is_rejected(listed):
    with pytest.raises(UsageError, match="start at #1"):
        resolve(["#0"], listed)


def test_empty_token_list_is_rejected(listed):
    with pytest.raises(UsageError):
        resolve([], listed)


def test_resolve_one_rejects_a_range(listed):
    with pytest.raises(UsageError, match="takes one"):
        resolve_one("#1-3", listed)


def test_resolve_one_accepts_a_single(listed):
    assert resolve_one("#2", listed) == f"{2:016x}"


@pytest.mark.parametrize(
    "token,expected",
    [
        ("18f2a9c4b7d1e0aa", True),
        ("abc123def456", True),
        ("#1", False),
        ("3", False),
        ("not-an-id", False),
    ],
)
def test_looks_like_id(token, expected):
    assert looks_like_id(token) is expected
