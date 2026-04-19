"""AC1 — robots.txt compliance incl. wildcard behavior."""
from __future__ import annotations

import pytest


def test_ac1_disallow_all_blocks_every_path():
    from lavandula.reports.robots import can_fetch
    text = "User-agent: *\nDisallow: /\n"
    assert not can_fetch(text, "/")
    assert not can_fetch(text, "/annual-report")
    assert not can_fetch(text, "/reports/2024.pdf")


def test_ac1_disallow_prefix_under_star():
    from lavandula.reports.robots import can_fetch
    text = "User-agent: *\nDisallow: /reports/\n"
    assert not can_fetch(text, "/reports/2024.pdf")
    assert can_fetch(text, "/other/thing")


def test_ac1_specific_ua_allow_overrides_star_disallow():
    from lavandula.reports.robots import can_fetch
    text = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: Lavandula Design report crawler\nAllow: /annual/\n"
    )
    assert can_fetch(text, "/annual/x.pdf")
    assert not can_fetch(text, "/other/")


def test_ac1_no_ua_carveout_for_generic_wildcard():
    """When only User-agent: * is present, it always applies."""
    from lavandula.reports.robots import can_fetch
    text = "User-agent: *\nDisallow: /secret\n"
    assert not can_fetch(text, "/secret/file")
