"""AC7, AC11: robots.txt parsing and stanza matching."""
from pathlib import Path

import pytest

from lavandula.nonprofits import robots


F = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "cn"


def test_wildcard_only():
    text = (F / "robots-simple.txt").read_text()
    policy = robots.parse(text, ua="Lavandula Design research crawler/1.0")
    assert not policy.is_allowed("/search/foo")
    assert not policy.is_allowed("/profile/xyz")
    assert policy.is_allowed("/ein/530196605")


def test_named_stanza_wins_over_wildcard():
    text = (F / "robots-named-ua.txt").read_text()
    policy = robots.parse(
        text,
        ua="Lavandula Design research crawler/1.0",
    )
    # Named stanza allows /ein/ (it's not listed there); wildcard would have
    # blocked it.
    assert policy.is_allowed("/ein/530196605")
    assert not policy.is_allowed("/profile/xyz")
    assert policy.matched_agent == "Lavandula"


def test_tied_specificity_halts():
    text = (F / "robots-tied.txt").read_text()
    with pytest.raises(robots.AmbiguousRobots):
        robots.parse(text, ua="Mozilla crawler probe")


def test_disallowed_ein_is_blocked():
    text = (F / "robots-simple.txt").read_text()
    policy = robots.parse(text, ua="anyua")
    assert robots.is_ein_disallowed("863371262", policy)
    assert robots.is_ein_disallowed("86-3371262", policy)
    assert not robots.is_ein_disallowed("530196605", policy)


def test_allows_ein_path_probe():
    text = (F / "robots-simple.txt").read_text()
    policy = robots.parse(text, ua="x")
    assert robots.allows_ein_path(policy) is True


def test_new_ein_disallow_halts_probe():
    text = "User-agent: *\nDisallow: /ein/\n"
    policy = robots.parse(text, ua="x")
    assert robots.allows_ein_path(policy) is False
