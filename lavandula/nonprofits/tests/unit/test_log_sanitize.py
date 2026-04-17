"""AC21: control-character stripping + length cap on log writes."""
from lavandula.nonprofits.logging_utils import sanitize, sanitize_exception


def test_strips_crlf():
    assert sanitize("foo\r\nFAKE_LOG: bar") == "fooFAKE_LOG: bar"


def test_strips_other_controls():
    s = sanitize("foo\x00\x01\x02bar\x7f")
    assert s == "foobar"


def test_none_becomes_empty():
    assert sanitize(None) == ""


def test_truncates():
    s = "x" * 2000
    out = sanitize(s, max_len=100)
    assert len(out) < len(s)
    assert "truncated" in out


def test_exception_redacts_home(monkeypatch):
    import pathlib
    monkeypatch.setattr(pathlib.Path, "home", lambda: pathlib.Path("/home/redacted"))
    msg = sanitize_exception(ValueError("failed at /home/redacted/secret"))
    assert "/home/redacted" not in msg
    assert "~" in msg
