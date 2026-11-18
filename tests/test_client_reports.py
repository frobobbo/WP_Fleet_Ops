"""Client report exports must not mix another account's evidence into a report."""
import importlib
import warnings

import pytest


@pytest.fixture
def report_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "reports.sqlite3"))
    monkeypatch.setenv("WP_FLEET_OPS_REVISION", "client-report-test")
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        for name, url, account, uptime in (
            ("Selected Site", "https://selected.example", "Church Client", "true"),
            ("Outside Site", "https://outside.example", "Other Client", "false"),
            ("Unassigned Site", "https://unassigned.example", "", "true"),
        ):
            response = client.post(
                "/snapshot",
                data={
                    "name": name, "url": url, "client": account, "uptime_ok": uptime,
                    "ssl_days": 90, "wp_updates": 0, "backup_age_hours": 12,
                    "response_ms": 100, "security_header_count": 3,
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
        main.store.upsert_site("Selected Gap", "https://selected-gap.example", "Church Client")
        main.store.upsert_site("Outside Gap", "https://outside-gap.example", "Other Client")
        yield client, main


def report_text(response, path):
    return response.json()["report"] if path == "/api/report" else response.text


@pytest.mark.parametrize("path", ["/report", "/api/report"])
def test_reports_scope_observations_gaps_and_status_to_client(report_client, path):
    client, _ = report_client
    response = client.get(path, params={"client": "  Church Client  "})

    assert response.status_code == 200
    text = report_text(response, path)
    assert "Selected Site" in text
    assert "Selected Gap" in text
    assert "Monitoring gaps: 1" in text
    assert "Critical sites: 0" in text
    assert "Outside Site" not in text
    assert "Outside Gap" not in text
    assert "Unassigned Site" not in text
    assert "outside" not in text
    if path == "/api/report":
        payload = response.json()
        assert payload["client"] == "Church Client"
        assert payload["status"] == "yellow"
        assert payload["tracked_site_count"] == 2
        assert payload["site_count"] == payload["care_check_count"] == payload["current_evidence_count"] == 1
        assert payload["monitoring_gap_count"] == 1
        assert payload["line_count"] == len(text.splitlines())


@pytest.mark.parametrize("path", ["/report", "/api/report"])
def test_reports_scope_unassigned_account_without_other_clients(report_client, path):
    client, _ = report_client
    response = client.get(path, params={"client": "  uNaSsIgNeD  "})

    assert response.status_code == 200
    text = report_text(response, path)
    assert "Unassigned Site" in text
    assert "Selected Site" not in text
    assert "Outside Site" not in text
    assert "Monitoring Evidence Gaps" not in text
    if path == "/api/report":
        payload = response.json()
        assert payload["client"] == "Unassigned"
        assert payload["status"] == "green"
        assert payload["tracked_site_count"] == payload["current_evidence_count"] == 1
        assert payload["monitoring_gap_count"] == 0


@pytest.mark.parametrize("path", ["/report", "/api/report"])
def test_reports_without_client_keep_complete_fleet_export(report_client, path):
    client, _ = report_client
    response = client.get(path)

    assert response.status_code == 200
    text = report_text(response, path)
    for name in ("Selected Site", "Selected Gap", "Outside Site", "Outside Gap", "Unassigned Site"):
        assert name in text
    if path == "/api/report":
        payload = response.json()
        assert payload["client"] is None
        assert payload["status"] == "red"
        assert payload["tracked_site_count"] == 5
        assert payload["current_evidence_count"] == 3
        assert payload["monitoring_gap_count"] == 2


@pytest.mark.parametrize("path", ["/report", "/api/report"])
def test_reports_reject_unknown_client_before_reading_observations(report_client, monkeypatch, path):
    client, main = report_client

    def unexpected_read():
        pytest.fail("unknown report account must be rejected before observation reads")

    monkeypatch.setattr(main.store, "latest_dashboard", unexpected_read)
    monkeypatch.setattr(main.store, "latest_care_checks", unexpected_read)
    response = client.get(path, params={"client": "Missing Client"})

    assert response.status_code == 404
    assert response.json() == {"detail": "No tracked sites found for client 'Missing Client'."}


@pytest.mark.parametrize("path", ["/report", "/api/report"])
@pytest.mark.parametrize("account", ["", "  ", "x" * 201, "Invalid\nClient"])
def test_reports_reject_invalid_client_before_inventory_reads(report_client, monkeypatch, path, account):
    client, main = report_client

    def unexpected_read():
        pytest.fail("invalid report account must be rejected before any inventory read")

    monkeypatch.setattr(main.store, "list_sites", unexpected_read)
    response = client.get(path, params={"client": account})

    assert response.status_code == 422
    assert set(response.json()) == {"detail"}


@pytest.mark.parametrize("path", ["/report", "/api/report"])
@pytest.mark.parametrize("table, timestamp", [("care_checks", "checked_at"), ("snapshots", "captured_at")])
def test_scoped_reports_keep_paired_freshness_gate(report_client, path, table, timestamp):
    from contextlib import closing

    client, main = report_client
    with closing(main.store._connect()) as connection, connection:
        connection.execute(
            f"update {table} set {timestamp} = '2000-01-01 00:00:00' "
            "where site_id = (select id from sites where url = ?)",
            ("https://selected.example",),
        )
    response = client.get(path, params={"client": "Church Client"})

    assert response.status_code == 200
    text = report_text(response, path)
    assert "Sites reviewed: 0" in text
    assert "Monitoring gaps: 2" in text
    assert "Selected Site" in text
    assert "Outside Site" not in text
    assert "Unassigned Site" not in text
    if path == "/api/report":
        payload = response.json()
        assert payload["status"] == "yellow"
        assert payload["tracked_site_count"] == 2
        assert payload["current_evidence_count"] == payload["care_check_count"] == 0
        assert payload["monitoring_gap_count"] == 2
