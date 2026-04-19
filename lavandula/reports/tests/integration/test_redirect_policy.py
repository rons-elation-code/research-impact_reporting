"""AC12.2 — final-URL eTLD+1 gating; AC12.2.1 — every-hop gating."""
from __future__ import annotations

import pytest


def test_ac12_2_final_etld1_match_allowed():
    from lavandula.reports.redirect_policy import check_redirect_chain
    chain = ["https://nonprofit.org/a", "https://www.nonprofit.org/b/annual.pdf"]
    assert check_redirect_chain(chain, seed_etld1="nonprofit.org").ok


def test_ac12_2_final_platform_allowed():
    from lavandula.reports.redirect_policy import check_redirect_chain
    chain = [
        "https://nonprofit.org/a",
        "https://issuu.com/nonprofit/docs/annual-2024",
    ]
    out = check_redirect_chain(chain, seed_etld1="nonprofit.org")
    assert out.ok


def test_ac12_2_1_intermediate_hop_blocked():
    """AC12.2.1 — intermediate attacker.com hop is rejected even if final is platform."""
    from lavandula.reports.redirect_policy import check_redirect_chain
    chain = [
        "https://nonprofit.org/x",
        "https://attacker.com/track?u=nonprofit.org",
        "https://issuu.com/whoever/docs/x",
    ]
    out = check_redirect_chain(chain, seed_etld1="nonprofit.org")
    assert not out.ok
    assert out.reason == "cross_origin_blocked"


def test_ac12_2_cross_origin_blocks():
    from lavandula.reports.redirect_policy import check_redirect_chain
    chain = [
        "https://nonprofit.org/x",
        "https://attacker.com/fake.pdf",
    ]
    out = check_redirect_chain(chain, seed_etld1="nonprofit.org")
    assert not out.ok


def test_ac12_2_1_too_many_redirects():
    from lavandula.reports.redirect_policy import check_redirect_chain
    chain = [f"https://nonprofit.org/{i}" for i in range(10)]
    out = check_redirect_chain(chain, seed_etld1="nonprofit.org")
    assert not out.ok
    assert "redirect_chain_too_long" in (out.note or "")
