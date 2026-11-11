"""Regression coverage for RFC 6797 quoted max-age values and evidence flow."""

from email.message import Message
import importlib
import warnings

import pytest

from wp_fleet_ops.checks import evaluate_site


def check_headers(value, *, url="https://quoted-hsts.example"):
    return evaluate_site(
        "Quoted HSTS", url, 200, 200, 90, "6.8", 0, 24,
        {"strict-transport-security": value, "x-frame-options": "SAMEORIGIN"},
    )


@pytest.mark.parametrize(
    "value",
    [
        'max-age="31536000"',
        'max-age="1"',
        'max-age="00001"; includeSubDomains',
        ' MAX-AGE = "31536000" ; includeSubDomains ',
        r'max-age="\3\1\5\3\6\0\0\0"',
        r'max-age="31536\000"',
        'max-age="' + "1" + "0" * 5000 + '"',
    ],
    ids=["quoted", "one-second", "leading-zeros", "case-and-whitespace", "quoted-pairs", "mixed-pairs", "large"],
)
def test_quoted_hsts_max_age_retains_effective_evidence(value):
    check = check_headers(value)

    assert check.security_headers["strict-transport-security"] == value
    assert check.score == 100
    assert check.actions == []


@pytest.mark.parametrize(
    "value",
    [
        'max-age="0"',
        'max-age="000"',
        r'max-age="\0\0"',
        'max-age=""',
        'max-age="-1"',
        'max-age="+1"',
        'max-age="１２３"',
        'max-age=" 123 "',
        'max-age="123',
        'max-age=123"',
        'max-age="12"3"',
        r'max-age="123\"',
        r'max-age="12\x33"',
        r'max-age=\123',
        'max-age="123"; MAX-AGE=456',
        'max-age=456; max-age="123"',
    ],
)
def test_quoted_hsts_does_not_credit_disabled_or_invalid_values(value):
    check = check_headers(value)

    assert check.security_headers == {"x-frame-options": "SAMEORIGIN"}
    assert check.score == 96
    assert check.actions == ["Add or verify HSTS security header."]


def test_quoted_hsts_remains_ineffective_on_plain_http():
    check = check_headers('max-age="31536000"', url="http://quoted-hsts.example")

    assert check.security_headers == {"x-frame-options": "SAMEORIGIN"}
    assert check.score == 96
    assert check.actions == ["Add or verify HSTS security header."]


@pytest.mark.parametrize("first_value", ['max-age="0"', 'max-age="31536000"'])
def test_quoted_hsts_still_uses_only_the_first_response_field(first_value):
    headers = Message()
    headers.add_header("Strict-Transport-Security", first_value)
    headers.add_header("Strict-Transport-Security", 'max-age="31536000"')
    headers.add_header("X-Frame-Options", "SAMEORIGIN")
    check = evaluate_site(
        "Duplicate HSTS", "https://quoted-hsts.example", 200, 200, 90,
        "6.8", 0, 24, headers,  # type: ignore[arg-type]
    )

    expected_hsts = {} if first_value == 'max-age="0"' else {"strict-transport-security": first_value}
    assert check.security_headers == {**expected_hsts, "x-frame-options": "SAMEORIGIN"}
    assert check.score == (100 if expected_hsts else 96)


@pytest.mark.parametrize("value", ['max-age="31536000"', r'max-age="\31536000"'])
def test_fetched_quoted_hsts_survives_paired_persistence_and_reports(tmp_path, monkeypatch, value):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("WP_FLEET_OPS_REVISION", "quoted-hsts-test")
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main
    import wp_fleet_ops.checks as checks

    importlib.reload(main)
    headers = Message()
    headers.add_header("Strict-Transport-Security", value)
    headers.add_header("X-Frame-Options", "SAMEORIGIN")

    class Response:
        status = 200

        def __init__(self):
            self.headers = headers

        def geturl(self):
            return "https://quoted-hsts.example"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(checks.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(checks, "ssl_days_remaining", lambda *_args, **_kwargs: 90)
    with TestClient(main.app) as client:
        response = client.post(
            "/care/fetch-check",
            data={"name": "Quoted HSTS", "url": "https://quoted-hsts.example"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        care = client.get("/api/care-check-history").json()["care_checks"][0]
        snapshot = client.get("/api/snapshot-history").json()["snapshots"][0]
        assert care["security_headers"]["strict-transport-security"] == value
        assert care["score"] == snapshot["score"] == 100
        assert care["actions"] == snapshot["alerts"] == []
        assert snapshot["security_header_count"] == 2
        assert client.get("/api/actions").json()["action_count"] == 0
        for path in ("/report", "/api/report"):
            response = client.get(path)
            assert response.status_code == 200
            report = response.json()["report"] if path == "/api/report" else response.text
            assert "Add or verify HSTS" not in report
            assert "missing HSTS" not in report
            assert "Score: 100/100" in report
