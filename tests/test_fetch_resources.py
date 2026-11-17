"""Live checks must release error responses without waiting for GC."""
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError

import pytest

from wp_fleet_ops import checks


@pytest.mark.parametrize("status", [404, 503])
@pytest.mark.parametrize(
    "final_url, expected_status, expected_headers",
    [
        ("http://unavailable.example", None, {}),
        (
            "https://unavailable.example",
            None,
            {"strict-transport-security": "max-age=31536000"},
        ),
        ("https://user:password@invalid.example", 0, {}),
    ],
    ids=["http", "https", "invalid-redirect"],
)
def test_live_check_closes_http_error_before_certificate_probe(
    monkeypatch, status, final_url, expected_status, expected_headers,
):
    headers = Message()
    headers["Strict-Transport-Security"] = "max-age=31536000"
    headers["Set-Cookie"] = "session=not-monitoring-evidence"
    body = BytesIO(b"error response body")
    # Keep a reference so garbage collection cannot make cleanup appear correct.
    error = HTTPError(final_url, status, "Unavailable", headers, body)

    def unavailable(*_args, **_kwargs):
        raise error

    def certificate_days(*_args, **_kwargs):
        assert body.closed
        return 60

    monkeypatch.setattr(checks.urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(checks, "ssl_days_remaining", certificate_days)
    try:
        check = checks.fetch_basic_site_check("Unavailable", "https://origin.example")
        assert body.closed
        assert error.closed
        assert check.http_status == (status if expected_status is None else expected_status)
        assert check.security_headers == expected_headers
        assert check.url == "https://origin.example"
    finally:
        error.close()


def test_live_check_closes_http_error_when_header_processing_fails(monkeypatch):
    headers = Message()
    headers["X-Frame-Options"] = "DENY"
    body = BytesIO(b"error response body")
    error = HTTPError("https://unavailable.example", 503, "Unavailable", headers, body)

    def unavailable(*_args, **_kwargs):
        raise error

    def broken_headers(*_args, **_kwargs):
        raise RuntimeError("header processing failed")

    monkeypatch.setattr(checks.urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(checks, "_monitored_security_headers", broken_headers)
    try:
        with pytest.raises(RuntimeError, match="header processing failed"):
            checks.fetch_basic_site_check("Unavailable", "https://origin.example")
        assert body.closed
        assert error.closed
    finally:
        error.close()
