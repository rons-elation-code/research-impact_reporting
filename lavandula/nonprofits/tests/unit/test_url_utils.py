"""EIN canonicalization tests — filesystem-safety gate."""
import pytest

from lavandula.nonprofits.url_utils import canonicalize_ein, ein_from_profile_url


def test_canonicalize_strips_dash():
    assert canonicalize_ein("53-0196605") == "530196605"


def test_canonicalize_accepts_plain():
    assert canonicalize_ein("530196605") == "530196605"


def test_rejects_path_traversal():
    with pytest.raises(ValueError):
        canonicalize_ein("../../etc/passwd")


def test_rejects_too_short():
    with pytest.raises(ValueError):
        canonicalize_ein("12345")


def test_rejects_too_long():
    with pytest.raises(ValueError):
        canonicalize_ein("1234567890")


def test_rejects_non_digit():
    with pytest.raises(ValueError):
        canonicalize_ein("12345678a")


def test_extracts_from_profile_url():
    assert ein_from_profile_url(
        "https://www.charitynavigator.org/ein/530196605"
    ) == "530196605"


def test_ignores_non_profile_url():
    assert ein_from_profile_url(
        "https://www.charitynavigator.org/search/foo"
    ) is None
