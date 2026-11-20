"""The score indicator must not imply healthy monitoring from a high average."""
import importlib
import warnings
from contextlib import closing
from html.parser import HTMLParser

import pytest


@pytest.fixture
def status_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "status.sqlite3"))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main


class ScoreIndicatorParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.attrs = None
        self.parts = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and "status-orb" in (attrs.get("class") or "").split():
            self.attrs = attrs
            self.depth = 1
        elif self.depth and tag == "div":
            self.depth += 1

    def handle_endtag(self, tag):
        if self.depth and tag == "div":
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def capture(client, name="Status Site", updates=0):
    response = client.post("/snapshot", data={
        "name": name, "url": f"https://{name.lower().replace(' ', '-')}.example",
        "client": "Care Client", "uptime_ok": "true", "ssl_days": 90,
        "wp_updates": updates, "backup_age_hours": 12,
        "response_ms": 100, "security_header_count": 3,
    }, follow_redirects=False)
    assert response.status_code == 303


def assert_indicator(client, status, label):
    summary = client.get("/api/summary").json()
    assert summary["overall_status"] == status
    response = client.get("/")
    assert response.status_code == 200
    parser = ScoreIndicatorParser()
    parser.feed(response.text)
    assert parser.attrs is not None
    assert parser.attrs.get("data-fleet-status") == status
    assert status in (parser.attrs["class"] or "").split()
    text = " ".join(" ".join(parser.parts).split())
    assert f"Fleet status: {label}" in text
    assert "current average fleet score" in text
    expected_score = summary["average_score"]
    assert text.startswith(str(expected_score) if expected_score is not None else "—")


@pytest.mark.parametrize("updates,status,label", [
    (0, "green", "Healthy"),
    (1, "yellow", "Review needed"),
    (5, "red", "Needs attention"),
])
def test_score_indicator_uses_paired_status_not_just_average(status_client, updates, status, label):
    client, main = status_client
    capture(client, updates=updates)
    # Five pending updates are critical even though the recorded score is 85.
    assert main.store.latest_dashboard()[0]["score"] >= 85
    before = (main.store.latest_dashboard(), main.store.latest_care_checks())
    assert_indicator(client, status, label)
    assert (main.store.latest_dashboard(), main.store.latest_care_checks()) == before


@pytest.mark.parametrize("score,saved_status,status,label", [
    (40, "green", "red", "Needs attention"),
    (100, "red", "red", "Needs attention"),
    (75, "green", "yellow", "Review needed"),
    (100, "yellow", "yellow", "Review needed"),
])
def test_score_indicator_preserves_corrected_or_retained_care_risk(
    status_client, score, saved_status, status, label,
):
    client, main = status_client
    capture(client)
    with closing(main.store._connect()) as connection, connection:
        connection.execute("update care_checks set score = ?, status = ?", (score, saved_status))
    assert client.get("/api/summary").json()["average_score"] == 100
    assert_indicator(client, status, label)


@pytest.mark.parametrize("table,timestamp", [
    ("snapshots", "captured_at"), ("care_checks", "checked_at"),
])
@pytest.mark.parametrize("value", [
    "2000-01-01 00:00:00", "invalid timestamp", "9999-01-01 00:00:00", None,
])
def test_score_indicator_does_not_escalate_incomplete_historical_risk(
    status_client, table, timestamp, value,
):
    client, main = status_client
    capture(client, updates=5)
    with closing(main.store._connect()) as connection, connection:
        if value is None:
            connection.execute(f"delete from {table}")
        else:
            connection.execute(f"update {table} set {timestamp} = ?", (value,))
    assert_indicator(client, "yellow", "Review needed")


def test_score_indicator_warns_for_empty_or_registry_only_inventory(status_client):
    client, main = status_client
    assert_indicator(client, "yellow", "Review needed")
    main.store.upsert_site("New Site", "https://new.example", "New Client")
    assert_indicator(client, "yellow", "Review needed")


def test_current_critical_risk_outranks_another_sites_monitoring_gap(status_client):
    client, main = status_client
    capture(client, updates=5)
    main.store.upsert_site("New Site", "https://new.example", "New Client")
    assert_indicator(client, "red", "Needs attention")


def test_score_indicator_background_uses_each_status_palette(status_client):
    client, _ = status_client
    html = client.get("/").text
    assert ".status-orb.green{--status-rgb:35,197,94}" in html
    assert ".status-orb.yellow{--status-rgb:245,158,11}" in html
    assert ".status-orb.red{--status-rgb:244,63,94}" in html
    assert "rgba(var(--status-rgb),.36)" in html
    assert "rgba(var(--status-rgb),.18)" in html
