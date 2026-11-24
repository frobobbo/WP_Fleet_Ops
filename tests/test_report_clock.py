"""Report timestamps must identify the same instant used to admit current evidence."""
import importlib
import warnings
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def report_clock_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "report-clock.sqlite3"))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main


def advancing_report_clock(monkeypatch, main, evaluated_at):
    import wp_fleet_ops.fleet as fleet

    class ReportClock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            instant = evaluated_at + timedelta(seconds=cls.calls)
            cls.calls += 1
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(main, "datetime", ReportClock)
    monkeypatch.setattr(fleet, "datetime", ReportClock)
    return ReportClock


@pytest.mark.parametrize("path, download", [
    ("/api/report", False), ("/report", False), ("/report", True),
])
@pytest.mark.parametrize("account", [None, "Clock Client"])
@pytest.mark.parametrize("past_boundary", [False, True])
def test_report_uses_one_clock_for_freshness_markdown_and_metadata(
    report_clock_client, monkeypatch, path, download, account, past_boundary,
):
    client, main = report_clock_client
    response = client.post(
        "/snapshot",
        data={
            "name": "Boundary Site", "url": "https://clock.example", "client": "Clock Client",
            "uptime_ok": "true", "ssl_days": 90, "wp_updates": 0,
            "backup_age_hours": 12, "response_ms": 100, "security_header_count": 3,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    evaluated_at = datetime(2026, 11, 24, 3, 44, 0, 123456, tzinfo=timezone.utc)
    observed_at = evaluated_at - timedelta(hours=main.SNAPSHOT_FRESHNESS_HOURS)
    if past_boundary:
        observed_at -= timedelta(microseconds=1)
    with closing(main.store._connect()) as connection, connection:
        connection.execute("update snapshots set captured_at = ?", (observed_at.isoformat(),))
        connection.execute("update care_checks set checked_at = ?", (observed_at.isoformat(),))
    before = (main.store.latest_dashboard(), main.store.latest_care_checks())
    clock = advancing_report_clock(monkeypatch, main, evaluated_at)
    params = {"download": "true"} if download else {}
    if account is not None:
        params["client"] = account
    response = client.get(path, params=params)

    assert response.status_code == 200
    payload = response.json() if path == "/api/report" else None
    text = payload["report"] if payload is not None else response.text
    assert [line for line in text.splitlines() if line.startswith("Generated:")] == [
        f"Generated: {evaluated_at.isoformat()}",
    ]
    assert clock.calls == 1, "Rendering and metadata must reuse the freshness-evaluation clock."
    expected_current = 0 if past_boundary else 1
    assert f"Sites reviewed: {expected_current}" in text
    assert f"Sites monitored: {expected_current}" in text
    assert ("Monitoring gaps: 1" in text) == past_boundary
    if payload is not None:
        assert payload["generated_at"] == evaluated_at.isoformat()
        assert payload["current_evidence_count"] == expected_current
        assert payload["monitoring_gap_count"] == int(past_boundary)
        assert payload["status"] == ("yellow" if past_boundary else "green")
        assert payload["line_count"] == len(text.splitlines())
    if download:
        assert response.headers["content-disposition"].startswith("attachment;")
    assert (main.store.latest_dashboard(), main.store.latest_care_checks()) == before


@pytest.mark.parametrize("registered_gap", [False, True])
def test_unverified_report_also_shares_its_evaluation_time(
    report_clock_client, monkeypatch, registered_gap,
):
    client, main = report_clock_client
    if registered_gap:
        main.store.upsert_site("Missing Evidence", "https://missing-clock.example", "Clock Client")
    evaluated_at = datetime(2026, 11, 24, 3, 44, 0, tzinfo=timezone.utc)
    clock = advancing_report_clock(monkeypatch, main, evaluated_at)
    response = client.get("/api/report")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"] == evaluated_at.isoformat()
    assert f"Generated: {payload['generated_at']}\n" in payload["report"]
    assert clock.calls == 1
    assert payload["status"] == "yellow"
    assert payload["current_evidence_count"] == 0
    assert payload["monitoring_gap_count"] == int(registered_gap)
