"""Unit tests for enrich_990.py CLI (Spec 0026)."""
from __future__ import annotations

import argparse

import pytest

from lavandula.nonprofits.tools.enrich_990 import (
    _validate_cache_dir,
    _validate_ein,
    _validate_state,
    _validate_years,
)


class TestValidateEIN:
    """AC42: CLI validates --ein."""

    def test_valid_ein(self):
        assert _validate_ein("123456789") == "123456789"

    def test_too_short(self):
        with pytest.raises(argparse.ArgumentTypeError, match="9 digits"):
            _validate_ein("12345")

    def test_too_long(self):
        with pytest.raises(argparse.ArgumentTypeError, match="9 digits"):
            _validate_ein("1234567890")

    def test_non_numeric(self):
        with pytest.raises(argparse.ArgumentTypeError, match="9 digits"):
            _validate_ein("12345678a")


class TestValidateState:
    """AC42: CLI validates --state."""

    def test_valid_state(self):
        assert _validate_state("NY") == "NY"

    def test_lowercase(self):
        with pytest.raises(argparse.ArgumentTypeError, match="uppercase"):
            _validate_state("ny")

    def test_too_long(self):
        with pytest.raises(argparse.ArgumentTypeError, match="uppercase"):
            _validate_state("NYC")


class TestValidateYears:
    """AC42: CLI validates --years."""

    def test_single_year(self):
        assert _validate_years("2024") == [2024]

    def test_multiple_years(self):
        assert _validate_years("2020,2021,2022") == [2020, 2021, 2022]

    def test_invalid_year(self):
        with pytest.raises(argparse.ArgumentTypeError, match="4 digits"):
            _validate_years("20")

    def test_mixed_invalid(self):
        with pytest.raises(argparse.ArgumentTypeError, match="4 digits"):
            _validate_years("2024,abc")


class TestValidateCacheDir:
    """AC42: CLI validates --cache-dir."""

    def test_valid_dir(self, tmp_path):
        assert _validate_cache_dir(str(tmp_path)) == tmp_path

    def test_nonexistent(self):
        with pytest.raises(argparse.ArgumentTypeError, match="does not exist"):
            _validate_cache_dir("/nonexistent/path/xyz")

    def test_symlink_rejected(self, tmp_path):
        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)
        with pytest.raises(argparse.ArgumentTypeError, match="symlink"):
            _validate_cache_dir(str(link))
