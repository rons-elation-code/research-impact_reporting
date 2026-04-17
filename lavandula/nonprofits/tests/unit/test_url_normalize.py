"""AC18-AC19: website URL normalization 10-rule pipeline."""
from lavandula.nonprofits.url_normalize import normalize


def test_baseline_noop():
    url, reason = normalize("https://example.org")
    assert url == "https://example.org"
    assert reason is None


def test_lowercase_host_and_default_port():
    url, reason = normalize("HTTPS://Redcross.Org:443/?fbclid=abc")
    assert url == "https://redcross.org"
    assert reason is None


def test_strip_utm_params_keeps_others():
    url, reason = normalize(
        "https://example.org/foo?utm_source=x&keep=1&fbclid=y"
    )
    assert url == "https://example.org/foo?keep=1"
    assert reason is None


def test_cn_redirect_unwrap():
    url, reason = normalize(
        "https://www.charitynavigator.org/redirect?to=https%3A%2F%2Fredcross.org%2F"
    )
    assert url == "https://redcross.org"
    assert reason is None


def test_idn_punycode():
    url, reason = normalize("https://bücher.example/foo")
    assert reason is None
    assert url is not None
    assert "xn--" in url


def test_mailto_rejected():
    url, reason = normalize("mailto:info@example.org")
    assert url is None
    assert reason == "mailto"


def test_tel_rejected():
    url, reason = normalize("tel:+15555551234")
    assert url is None
    assert reason == "tel"


def test_javascript_rejected():
    url, reason = normalize("javascript:alert(1)")
    assert url is None
    assert reason == "invalid"


def test_empty_missing():
    url, reason = normalize("")
    assert url is None
    assert reason == "missing"


def test_none_missing():
    url, reason = normalize(None)
    assert url is None
    assert reason == "missing"


def test_social_rejected_facebook():
    url, reason = normalize("https://www.facebook.com/someorg")
    assert url is None
    assert reason == "social"


def test_social_rejected_twitter():
    url, reason = normalize("https://twitter.com/someorg")
    assert url is None
    assert reason == "social"


def test_path_trailing_slash_root_only():
    url, reason = normalize("https://example.org/")
    assert url == "https://example.org"
    url2, _ = normalize("https://example.org/foo/")
    assert url2 == "https://example.org/foo/"


def test_fragment_dropped():
    url, reason = normalize("https://example.org/foo#section")
    assert url == "https://example.org/foo"
    assert reason is None


def test_wrapped_with_tracking_combined():
    url, reason = normalize(
        "https://www.charitynavigator.org/redirect?to=https%3A%2F%2Fexample.org%2F%3Futm_source%3Dcn"
    )
    assert url == "https://example.org"
    assert reason is None
