"""Historical dashboard alerts must not look like current operational findings."""
import importlib
import warnings
from contextlib import closing

import pytest


@pytest.fixture
def alert_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "alerts.sqlite3"))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main


def capture(client, severity):
    response = client.post("/snapshot", data={
        "name": "Alert Site", "url": "https://alerts.example", "client": "Care Client",
        "uptime_ok": "true", "ssl_days": 90,
        "wp_updates": 5 if severity == "critical" else 1 if severity == "warning" else 0,
        "backup_age_hours": 12, "response_ms": 100,
        "security_header_count": 0 if severity == "info" else 3,
    }, follow_redirects=False)
    assert response.status_code == 303


def fleet_alert_cell(client):
    response = client.get("/")
    assert response.status_code == 200
    fleet = response.text.split('id="fleet"', 1)[1].split("</tbody>", 1)[0]
    return fleet.rsplit("<td>", 1)[1].split("</td>", 1)[0]


@pytest.mark.parametrize("severity", ["critical", "warning", "info", None])
@pytest.mark.parametrize("table,timestamp", [
    ("snapshots", "captured_at"), ("care_checks", "checked_at"),
])
@pytest.mark.parametrize("value", [
    "2000-01-01 00:00:00", "not-a-timestamp", "9999-01-01 00:00:00",
])
def test_incomplete_paired_evidence_labels_alerts_as_neutral_history(
    alert_client, severity, table, timestamp, value,
):
    client, main = alert_client
    capture(client, severity)
    with closing(main.store._connect()) as connection, connection:
        connection.execute(f"update {table} set {timestamp} = ?", (value,))
    snapshots_before = main.store.latest_dashboard()
    care_before = main.store.latest_care_checks()

    cell = fleet_alert_cell(client)

    if severity is None:
        assert '<span class="tag">observed clear</span>' in cell
    else:
        for alert in snapshots_before[0]["alerts"]:
            assert f'<span class="tag">observed {alert["severity"]} · {alert["message"]}</span>' in cell
    for color in ("green", "red", "yellow", "blue"):
        assert f'class="tag {color}"' not in cell
    assert main.store.latest_dashboard() == snapshots_before
    assert main.store.latest_care_checks() == care_before
    summary = client.get("/api/summary").json()
    assert summary["monitoring_gap_count"] == 1
    assert summary["critical_alerts"] == summary["needs_attention"] == 0


@pytest.mark.parametrize("severity", ["critical", "warning", "info", None])
def test_missing_care_check_keeps_snapshot_alerts_historical(alert_client, severity):
    client, main = alert_client
    capture(client, severity)
    with closing(main.store._connect()) as connection, connection:
        connection.execute("delete from care_checks")
    cell = fleet_alert_cell(client)
    assert f'class="tag">observed {severity or "clear"}' in cell
    assert 'class="tag green"' not in cell
    assert 'class="tag red"' not in cell


@pytest.mark.parametrize("severity,color", [
    ("critical", "red"), ("warning", "yellow"), ("info", "blue"), (None, "green"),
])
def test_current_paired_evidence_preserves_alert_severity_and_clear_badge(
    alert_client, severity, color,
):
    client, _ = alert_client
    capture(client, severity)
    cell = fleet_alert_cell(client)
    assert f'class="tag {color}">{severity or "clear"}' in cell
    assert "observed" not in cell


def test_fresh_combined_capture_restores_current_alert_presentation(alert_client):
    client, main = alert_client
    capture(client, "critical")
    with closing(main.store._connect()) as connection, connection:
        connection.execute("update care_checks set checked_at = '2000-01-01 00:00:00'")
    assert 'class="tag">observed critical' in fleet_alert_cell(client)
    capture(client, "critical")
    assert 'class="tag red">critical' in fleet_alert_cell(client)
    assert client.get("/api/summary").json()["critical_alerts"] == 1


def test_historical_alerts_still_escape_persisted_messages(alert_client):
    import json

    client, main = alert_client
    capture(client, "critical")
    alerts = [{"site": "Alert Site", "severity": "critical", "message": "<script>alert(1)</script>"}]
    with closing(main.store._connect()) as connection, connection:
        connection.execute("update snapshots set alerts_json = ?", (json.dumps(alerts),))
        connection.execute("update care_checks set checked_at = '2000-01-01 00:00:00'")
    cell = fleet_alert_cell(client)
    assert "observed critical · &lt;script&gt;alert(1)&lt;/script&gt;" in cell
    assert "<script>" not in cell
