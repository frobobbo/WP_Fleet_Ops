import os
import sqlite3
import warnings
from datetime import datetime, timedelta, timezone

import pytest


def make_test_client(tmp_path):
    os.environ["WP_FLEET_OPS_DB"] = str(tmp_path / "test.sqlite3")
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import importlib
    import wp_fleet_ops.main as main

    importlib.reload(main)
    return TestClient(main.app)


def valid_snapshot_payload(**overrides):
    payload = {
        "name": "Test Site",
        "url": "https://test-site.example",
        "uptime_ok": "true",
        "ssl_days": "60",
        "wp_updates": "0",
        "backup_age_hours": "24",
        "response_ms": "250",
        "security_header_count": "3",
    }
    payload.update(overrides)
    return payload


def test_health_and_report_endpoints(tmp_path):
    client = make_test_client(tmp_path)
    assert client.get("/health").json() == {"status": "ok", "app": "wp-fleet-ops"}
    response = client.post("/care/manual-check", data={"name": "A", "url": "https://a.example", "client": "Client A"}, follow_redirects=False)
    assert response.status_code == 303
    report = client.get("/report").text
    assert "Monthly WordPress Care Report" in report
    assert "WP FleetOps Maintenance Report" in report


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/care/manual-check",
            {
                "name": "Oversized Care Reading",
                "url": "https://oversized-care.example",
                "latency_ms": str(1 << 63),
            },
        ),
        (
            "/snapshot",
            valid_snapshot_payload(
                name="Oversized Snapshot Reading",
                url="https://oversized-snapshot.example",
                response_ms=str(1 << 63),
            ),
        ),
    ],
)
def test_write_forms_reject_telemetry_too_large_for_sqlite_without_partial_state(tmp_path, path, payload):
    client = make_test_client(tmp_path)

    response = client.post(path, data=payload, follow_redirects=False)

    assert response.status_code == 422
    assert client.get("/ready").json() == {
        "status": "ready",
        "app": "wp-fleet-ops",
        "database": "ok",
        "sites": 0,
        "care_checks": 0,
        "fleet_snapshots": 0,
    }


def test_responses_include_browser_security_headers(tmp_path):
    client = make_test_client(tmp_path)

    for path in ("/", "/api/summary"):
        response = client.get(path)

        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "form-action 'self'" in response.headers["content-security-policy"]


def test_api_report_returns_structured_report_export(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Export Site", url="https://export.example", client="Client Export"),
        follow_redirects=False,
    )

    response = client.get("/api/report")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["site_count"] == 1
    assert payload["care_check_count"] == 1
    assert payload["line_count"] == len(payload["report"].splitlines())
    assert "Monthly WordPress Care Report" in payload["report"]
    assert "WP FleetOps Maintenance Report" in payload["report"]
    assert "Export Site" in payload["report"]
    assert payload["status"] == "green"


def test_api_report_status_reflects_current_site_risk(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Report Site",
            url="https://critical-report.example",
            uptime_ok="false",
            ssl_days="2",
            backup_age_hours="120",
        ),
        follow_redirects=False,
    )

    payload = client.get("/api/report").json()

    assert payload["monitoring_gap_count"] == 0
    assert payload["current_evidence_count"] == 1
    assert payload["status"] == "red"
    assert "Critical Report Site — Needs attention" in payload["report"]


def test_api_report_status_reflects_current_maintenance_risk(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Maintenance Report Site",
            url="https://maintenance-report.example",
            wp_updates="2",
        ),
        follow_redirects=False,
    )

    payload = client.get("/api/report").json()

    assert payload["monitoring_gap_count"] == 0
    assert payload["current_evidence_count"] == 1
    assert payload["status"] == "yellow"
    assert "[warning] 2 WordPress updates pending." in payload["report"]


def test_reports_fail_closed_on_missing_or_stale_combined_evidence(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Current Report Site",
            url="https://current-report.example",
            client="Current Client",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Expired Report Site",
            url="https://expired-report.example",
            client="Expired Client",
            uptime_ok="false",
            ssl_days="2",
            backup_age_hours="120",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Report Site",
            "url": "https://missing-report.example",
            "client": "Missing Client",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update snapshots set captured_at = ? where site_id = "
            "(select id from sites where url = ?)",
            ("2000-01-01 00:00:00", "https://expired-report.example"),
        )
        con.execute(
            "update care_checks set checked_at = ? where site_id = "
            "(select id from sites where url = ?)",
            ("2000-01-01T00:00:00+00:00", "https://expired-report.example"),
        )

    response = client.get("/api/report")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "yellow"
    assert payload["snapshot_freshness_threshold_hours"] == 168
    assert payload["tracked_site_count"] == 3
    assert payload["site_count"] == 1
    assert payload["care_check_count"] == 1
    assert payload["current_evidence_count"] == 1
    assert payload["monitoring_gap_count"] == 2
    assert "Current Report Site — Healthy" in payload["report"]
    assert "Expired Report Site — Needs attention" not in payload["report"]
    assert "SSL expires in 2 day(s)." not in payload["report"]
    assert "# Monitoring Evidence Gaps" in payload["report"]
    assert "Expired Report Site (Expired Client): fleet snapshot stale; care check stale." in payload["report"]
    assert "Missing Report Site (Missing Client): fleet snapshot missing; care check missing." in payload["report"]
    plain_report = client.get("/report").text
    assert "Expired Report Site — Needs attention" not in plain_report
    assert "SSL expires in 2 day(s)." not in plain_report
    assert "Expired Report Site (Expired Client): fleet snapshot stale; care check stale." in plain_report
    assert "Missing Report Site (Missing Client): fleet snapshot missing; care check missing." in plain_report


def test_ready_reports_database_access_and_current_counts(tmp_path):
    client = make_test_client(tmp_path)
    client.post("/care/manual-check", data={"name": "Ready Site", "url": "https://ready.example"}, follow_redirects=False)

    payload = client.get("/ready").json()

    assert payload == {
        "status": "ready",
        "app": "wp-fleet-ops",
        "database": "ok",
        "sites": 1,
        "care_checks": 1,
        "fleet_snapshots": 1,
    }


def test_ready_returns_service_unavailable_when_database_probe_fails(tmp_path, monkeypatch):
    client = make_test_client(tmp_path)
    import wp_fleet_ops.main as main

    def unavailable_database():
        raise sqlite3.OperationalError("sensitive database path")

    monkeypatch.setattr(main.store, "health_counts", unavailable_database)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "app": "wp-fleet-ops",
        "database": "unavailable",
    }
    assert "sensitive database path" not in response.text


def test_api_summary_returns_dashboard_rollups(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Healthy Site", url="https://healthy.example", response_ms="250"),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Risky Site",
            url="https://risky.example",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="5",
            backup_age_hours="96",
            response_ms="2200",
            security_header_count="0",
        ),
        follow_redirects=False,
    )

    summary = client.get("/api/summary").json()

    assert summary["sites"] == 2
    assert summary["fleet_snapshots"] == 2
    assert summary["care_checks"] == 2
    assert summary["healthy_sites"] == 1
    assert summary["needs_attention"] == 1
    assert summary["average_score"] == 50
    assert summary["overall_status"] == "red"
    assert summary["generated_at"].endswith("+00:00")
    assert summary["last_snapshot_at"]
    assert summary["critical_alerts"] >= 1


def test_api_summary_marks_critical_alerts_red_even_when_average_score_is_yellow(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Expiring Certificate",
            url="https://certificate.example",
            ssl_days="5",
        ),
        follow_redirects=False,
    )

    summary = client.get("/api/summary").json()

    assert summary["average_score"] == 75
    assert summary["critical_alerts"] == 1
    assert summary["overall_status"] == "red"


def test_score_between_65_and_69_is_consistently_warning_not_critical(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Warning Threshold Site",
            url="https://warning-threshold.example",
            client="Client Warning Threshold",
            ssl_days="20",
            wp_updates="2",
            backup_age_hours="48",
            response_ms="1300",
        ),
        follow_redirects=False,
    )

    summary = client.get("/api/summary").json()
    site = client.get("/api/sites").json()["sites"][0]
    client_row = client.get("/api/clients").json()["clients"][0]
    watch = client.get("/api/site-watchlist").json()["sites"][0]
    scorecard = client.get("/api/site-scorecards").json()["sites"][0]
    page = client.get("/").text

    assert summary["average_score"] == 66
    assert summary["overall_status"] == "yellow"
    assert summary["needs_attention"] == 0
    assert site["status"] == "yellow"
    assert client_row["status"] == "yellow"
    assert client_row["needs_attention"] == 0
    assert watch["watch_status"] == "warning"
    assert scorecard["status"] == "warning"
    assert "below 65 score" in page


def test_api_summary_warns_when_tracked_sites_lack_snapshots(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Monitored Site", url="https://monitored.example"),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={"name": "Unmonitored Site", "url": "https://unmonitored.example"},
        follow_redirects=False,
    )

    summary = client.get("/api/summary").json()

    assert summary["sites"] == 2
    assert summary["fleet_snapshots"] == 1
    assert summary["monitored_site_count"] == 1
    assert summary["missing_snapshot_count"] == 1
    assert summary["monitoring_coverage_percent"] == 50
    assert summary["average_score"] == 100
    assert summary["overall_status"] == "yellow"


def test_api_summary_warns_when_healthy_snapshot_is_stale(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Stale Healthy Site", url="https://stale-healthy.example"),
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    summary = client.get("/api/summary").json()

    assert summary["average_score"] == 100
    assert summary["missing_snapshot_count"] == 0
    assert summary["stale_snapshot_count"] == 1
    assert summary["current_snapshot_count"] == 0
    assert summary["snapshot_freshness_percent"] == 0
    assert summary["overall_status"] == "yellow"


def test_api_summary_excludes_stale_care_checks_from_current_client_risk(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Care Evidence",
            url="https://stale-care-evidence.example",
        ),
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update care_checks set checked_at = ?, status = ?",
            ("2000-01-01T00:00:00+00:00", "red"),
        )

    summary = client.get("/api/summary").json()

    assert summary["care_checks"] == 1
    assert summary["monitored_care_check_count"] == 1
    assert summary["current_care_check_count"] == 0
    assert summary["stale_care_check_count"] == 1
    assert summary["missing_care_check_count"] == 0
    assert summary["care_check_freshness_percent"] == 0
    assert summary["client_risks"] == 0
    assert summary["overall_status"] == "yellow"


def test_dashboard_replaces_live_monitor_label_when_snapshot_is_stale(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Stale Dashboard Site", url="https://stale-dashboard.example"),
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    page = client.get("/").text

    assert "Live monitor" not in page
    assert "1 stale snapshot" in page


def test_api_sites_returns_latest_per_site_operational_status(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Healthy Site", url="https://healthy.example"),
        follow_redirects=False,
    )
    client.post(
        "/care/manual-check",
        data={
            "name": "Risky Site",
            "url": "https://risky.example",
            "http_status": "503",
            "latency_ms": "1800",
            "ssl_days_remaining": "5",
            "update_count": "4",
            "backup_age_hours": "96",
        },
        follow_redirects=False,
    )

    response = client.get("/api/sites")

    assert response.status_code == 200
    sites = response.json()["sites"]
    assert [site["name"] for site in sites] == ["Risky Site", "Healthy Site"]
    assert sites[0]["status"] == "red"
    assert sites[0]["score"] < sites[1]["score"]
    assert sites[0]["critical_alerts"] >= 1
    assert sites[0]["latest_snapshot_at"]
    assert sites[0]["snapshot_freshness"] == "current"
    assert sites[0]["snapshot_age_hours"] >= 0


def test_api_sites_marks_old_snapshots_stale(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Site",
            url="https://stale-site.example",
            uptime_ok="false",
            ssl_days="2",
        ),
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    site = client.get("/api/sites").json()["sites"][0]

    assert site["name"] == "Stale Site"
    assert site["status"] == "unknown"
    assert site["observed_status"] == "red"
    assert site["critical_alerts"] == 0
    assert site["observed_critical_alerts"] >= 1
    assert site["snapshot_freshness"] == "stale"
    assert site["snapshot_age_hours"] > 168


def test_api_site_directory_includes_sites_missing_initial_snapshots(tmp_path):
    client = make_test_client(tmp_path)
    client.post("/sites", data={"name": "Needs First Snapshot", "url": "https://needs-first.example", "client": "Client Missing"}, follow_redirects=False)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Tracked Site", url="https://tracked.example", client="Client Tracked"),
        follow_redirects=False,
    )

    response = client.get("/api/site-directory")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["site_count"] == 2
    assert payload["monitored_count"] == 1
    assert payload["missing_snapshot_count"] == 1
    assert [site["name"] for site in payload["sites"]] == ["Needs First Snapshot", "Tracked Site"]
    missing = payload["sites"][0]
    assert missing["client"] == "Client Missing"
    assert missing["monitoring_status"] == "missing_snapshot"
    assert missing["status"] == "unknown"
    assert missing["score"] is None
    assert missing["latest_snapshot_at"] is None
    assert missing["recommended_action"] == "Capture an initial fleet snapshot for this site."
    tracked = payload["sites"][1]
    assert tracked["monitoring_status"] == "monitored"
    assert tracked["status"] == "green"
    assert tracked["score"] >= 85
    assert tracked["latest_snapshot_at"]


def test_api_site_directory_surfaces_stale_snapshot_freshness(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Stale Directory Site", url="https://stale-directory.example"),
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    payload = client.get("/api/site-directory").json()

    assert payload["site_count"] == 1
    assert payload["snapshot_freshness_threshold_hours"] == 168
    assert payload["current_snapshot_count"] == 0
    assert payload["stale_snapshot_count"] == 1
    site = payload["sites"][0]
    assert site["name"] == "Stale Directory Site"
    assert site["monitoring_status"] == "monitored"
    assert site["status"] == "unknown"
    assert site["observed_status"] == "green"
    assert site["snapshot_freshness"] == "stale"
    assert site["snapshot_age_hours"] > 168
    assert site["recommended_action"] == "Capture a fresh fleet snapshot and verify site health."


def test_api_clients_rolls_up_account_health(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Client A Healthy", url="https://healthy-a.example", client="Client A"),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Client A Risky",
            url="https://risky-a.example",
            client="Client A",
            uptime_ok="false",
            ssl_days="2",
            wp_updates="7",
            backup_age_hours="120",
            response_ms="2500",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/care/manual-check",
        data={"name": "Unassigned", "url": "https://unassigned.example", "client": "", "http_status": "200"},
        follow_redirects=False,
    )

    response = client.get("/api/clients")

    assert response.status_code == 200
    clients = response.json()["clients"]
    assert [row["client"] for row in clients] == ["Client A", "Unassigned"]
    assert clients[0]["site_count"] == 2
    assert clients[0]["average_score"] < 85
    assert clients[0]["status"] == "red"
    assert clients[0]["critical_alerts"] >= 1
    assert clients[0]["needs_attention"] == 1
    assert clients[1]["site_count"] == 1


def test_api_clients_warns_about_account_monitoring_gaps(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Client Site",
            url="https://stale-client.example",
            client="Client Monitoring Gap",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Client Site",
            "url": "https://missing-client.example",
            "client": "Client Monitoring Gap",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    payload = client.get("/api/clients").json()

    assert payload["client_count"] == 1
    account = payload["clients"][0]
    assert account["client"] == "Client Monitoring Gap"
    assert account["site_count"] == 2
    assert account["monitored_site_count"] == 1
    assert account["missing_snapshot_count"] == 1
    assert account["current_snapshot_count"] == 0
    assert account["stale_snapshot_count"] == 1
    assert account["monitoring_coverage_percent"] == 50
    assert account["snapshot_freshness_percent"] == 0
    assert account["average_score"] == 100
    assert account["status"] == "yellow"


def test_api_clients_excludes_stale_risk_from_current_account_health(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Expired Client Risk",
            url="https://expired-client-risk.example",
            client="Client Expired Risk",
            uptime_ok="false",
            ssl_days="2",
            wp_updates="7",
            backup_age_hours="120",
            response_ms="2500",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    payload = client.get("/api/clients").json()

    account = payload["clients"][0]
    assert account["client"] == "Client Expired Risk"
    assert account["monitored_site_count"] == 1
    assert account["current_snapshot_count"] == 0
    assert account["stale_snapshot_count"] == 1
    assert account["average_score"] == 100
    assert account["healthy_sites"] == 0
    assert account["needs_attention"] == 0
    assert account["critical_alerts"] == 0
    assert account["status"] == "yellow"


def test_api_operator_handoff_summarizes_current_shift_priorities(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Checkout Down",
            url="https://checkout-down.example",
            client="Commerce Co",
            uptime_ok="false",
            ssl_days="4",
            wp_updates="6",
            backup_age_hours="96",
            response_ms="2200",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Stable Blog", url="https://stable-blog.example", client="Content Co"),
        follow_redirects=False,
    )

    response = client.get("/api/operator-handoff")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["status"] == "red"
    assert payload["site_count"] == 2
    assert payload["client_count"] == 2
    assert payload["critical_client_count"] == 1
    assert payload["immediate_action_count"] >= 1
    assert payload["open_action_count"] >= payload["immediate_action_count"]
    assert payload["headline"] == (
        f"Red: 1 critical client and {payload['immediate_action_count']} immediate "
        f"{'action' if payload['immediate_action_count'] == 1 else 'actions'} require operator follow-up."
    )
    assert payload["top_clients"][0]["client"] == "Commerce Co"
    assert payload["top_actions"][0]["client"] == "Commerce Co"
    assert payload["top_actions"][0]["urgency"] == "immediate"
    assert payload["handoff_notes"][0].startswith("Prioritize Commerce Co")
    assert payload["handoff_notes"][-1] == "Watch SLO objective: Sites reachable at 50.0% compliance."


def test_api_operator_handoff_does_not_escalate_healthy_clients(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Healthy Handoff Site",
            url="https://healthy-handoff.example",
            client="Healthy Client",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/operator-handoff")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "green"
    assert payload["critical_client_count"] == 0
    assert payload["immediate_action_count"] == 0
    assert payload["open_action_count"] == 0
    assert payload["handoff_notes"] == ["No client-level risks require handoff at this time."]


def test_api_sla_breaches_returns_sites_missing_operational_targets(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Checkout Incident",
            url="https://checkout-incident.example",
            client="Client Commerce",
            uptime_ok="false",
            ssl_days="5",
            backup_age_hours="96",
            response_ms="2100",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Certificate Watch",
            url="https://certificate-watch.example",
            client="Client TLS",
            ssl_days="13",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Seven Day Certificate",
            url="https://seven-day-certificate.example",
            client="Client TLS",
            ssl_days="7",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Compliant Site", url="https://compliant.example"),
        follow_redirects=False,
    )

    response = client.get("/api/sla-breaches")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["site_count"] == 4
    assert payload["breach_count"] == 3
    assert payload["critical_breach_count"] == 2
    assert [site["name"] for site in payload["sites"]] == [
        "Checkout Incident",
        "Seven Day Certificate",
        "Certificate Watch",
    ]
    assert payload["sites"][0]["breach_count"] == 4
    assert payload["sites"][0]["highest_severity"] == "critical"
    assert {breach["target"] for breach in payload["sites"][0]["breaches"]} == {
        "availability",
        "tls_certificate",
        "backup_freshness",
        "response_time",
    }
    assert payload["sites"][1]["breaches"][0]["target"] == "tls_certificate"
    assert payload["sites"][1]["breaches"][0]["severity"] == "critical"
    assert payload["sites"][2]["breaches"][0]["target"] == "tls_certificate"
    assert payload["sites"][2]["breaches"][0]["severity"] == "warning"


def test_api_sla_breaches_fails_closed_for_incomplete_snapshot_evidence(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Current SLA Breach",
            url="https://current-sla-breach.example",
            uptime_ok="false",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale SLA Breach",
            url="https://stale-sla-breach.example",
            uptime_ok="false",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Current SLA Compliant",
            url="https://current-sla-compliant.example",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing SLA Evidence",
            "url": "https://missing-sla-evidence.example",
            "client": "Client New",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("2000-01-01 00:00:00", "https://stale-sla-breach.example"),
        )

    response = client.get("/api/sla-breaches")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "red"
    assert payload["site_count"] == 4
    assert payload["current_evidence_count"] == 2
    assert payload["unknown_count"] == 2
    assert payload["breach_count"] == 1
    assert payload["critical_breach_count"] == 1
    assert payload["warning_breach_count"] == 0
    assert payload["sla_evidence_percent"] == 50
    assert [site["name"] for site in payload["sites"]] == [
        "Current SLA Breach",
        "Missing SLA Evidence",
        "Stale SLA Breach",
    ]

    current, missing, stale = payload["sites"]
    assert current["sla_status"] == "breached"
    assert current["snapshot_freshness"] == "current"
    assert current["highest_severity"] == "critical"
    assert current["breaches"][0]["target"] == "availability"
    assert current["last_observed_breaches"] == current["breaches"]
    assert missing["sla_status"] == "unknown"
    assert missing["snapshot_freshness"] == "missing"
    assert missing["latest_snapshot_at"] is None
    assert missing["breaches"] == []
    assert missing["last_observed_breaches"] is None
    assert missing["recommended_action"] == (
        "Capture an initial fleet snapshot before evaluating SLA compliance."
    )
    assert stale["sla_status"] == "unknown"
    assert stale["snapshot_freshness"] == "stale"
    assert stale["snapshot_age_hours"] > 168
    assert stale["breaches"] == []
    assert stale["last_observed_breaches"][0]["target"] == "availability"
    assert stale["recommended_action"] == (
        "Capture a fresh fleet snapshot before evaluating SLA compliance."
    )


def test_api_actions_returns_prioritized_client_work_queue(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Site",
            url="https://critical.example",
            client="Client C",
            uptime_ok="false",
            ssl_days="2",
            wp_updates="5",
            backup_age_hours="120",
            response_ms="2600",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Warning Site",
            url="https://warning.example",
            client="Client W",
            wp_updates="1",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/actions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["action_count"] >= 2
    actions = payload["actions"]
    assert actions[0]["site"] == "Critical Site"
    assert actions[0]["client"] == "Client C"
    assert actions[0]["severity"] == "critical"
    assert actions[0]["recommended_action"] == "Confirm site availability, hosting status, and recent deploys."
    warning_actions = [
        action for action in actions if action["severity"] == "warning" and action["site"] == "Warning Site"
    ]
    assert warning_actions
    assert warning_actions[0]["recommended_action"] == "Schedule WordPress core, plugin, and theme updates."
    assert actions[0]["score"] < warning_actions[0]["score"]


def test_stale_snapshot_alerts_do_not_create_current_actions_or_incidents(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Expired Incident Evidence",
            url="https://expired-incident-evidence.example",
            client="Client Expired Evidence",
            uptime_ok="false",
            ssl_days="2",
            wp_updates="5",
            backup_age_hours="120",
            response_ms="2600",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    actions = client.get("/api/actions").json()
    incidents = client.get("/api/incidents").json()
    workload = client.get("/api/client-workload").json()
    approvals = client.get("/api/maintenance-approval-packets").json()
    dispatch = client.get("/api/dispatch-summary").json()

    assert actions == {"action_count": 0, "actions": []}
    assert incidents["incident_count"] == 0
    assert incidents["incidents"] == []
    assert workload["open_action_count"] == 0
    assert workload["clients"] == []
    assert approvals["needed_count"] == 0
    assert approvals["packets"][0]["packet_needed"] is False
    assert dispatch["status"] == "yellow"
    assert dispatch["monitoring_gap_count"] == 1
    assert dispatch["next_queue"] == "monitoring"


def test_stale_critical_snapshot_does_not_pollute_current_priority_views(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Expired Priority Evidence",
            url="https://expired-priority-evidence.example",
            client="Client Expired Priority",
            uptime_ok="false",
            ssl_days="2",
            wp_updates="5",
            backup_age_hours="120",
            response_ms="2600",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    summary = client.get("/api/summary").json()
    watchlist = client.get("/api/site-watchlist").json()
    site_priorities = client.get("/api/site-priorities").json()
    client_priorities = client.get("/api/client-priorities").json()
    kpis = client.get("/api/operations-kpis").json()

    assert summary["overall_status"] == "yellow"
    assert summary["current_snapshot_count"] == 0
    assert summary["stale_snapshot_count"] == 1
    assert summary["critical_alerts"] == 0
    assert summary["needs_attention"] == 0
    assert summary["healthy_sites"] == 0
    assert summary["average_score"] == 100
    assert watchlist["watchlist_count"] == 0
    assert watchlist["critical_watch_count"] == 0
    assert watchlist["sites"] == []
    assert site_priorities["priority_site_count"] == 0
    assert site_priorities["returned_site_count"] == 0
    assert site_priorities["sites"] == []
    assert client_priorities["client_count"] == 0
    assert client_priorities["returned_client_count"] == 0
    assert client_priorities["total_priority_score"] == 0
    assert client_priorities["clients"] == []
    assert kpis["status"] == "yellow"
    assert kpis["average_score"] == 100
    assert kpis["red_site_count"] == 0
    assert kpis["yellow_site_count"] == 0
    assert kpis["green_site_count"] == 0
    assert kpis["open_action_count"] == 0
    assert kpis["priority_site_count"] == 0
    assert kpis["top_priority_site"] is None
    assert kpis["recommended_focus"] == "Refresh stale fleet snapshots before the next operations review."


def test_api_site_watchlist_returns_only_attention_sites(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Watch Site",
            url="https://critical-watch.example",
            client="Client Watch",
            uptime_ok="false",
            ssl_days="2",
            wp_updates="5",
            backup_age_hours="120",
            response_ms="2200",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Warning Watch Site",
            url="https://warning-watch.example",
            client="Client Watch",
            wp_updates="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Healthy Watch Site", url="https://healthy-watch.example"),
        follow_redirects=False,
    )

    response = client.get("/api/site-watchlist")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["site_count"] == 3
    assert payload["watchlist_count"] == 2
    assert payload["critical_watch_count"] == 1
    assert [site["name"] for site in payload["sites"]] == ["Critical Watch Site", "Warning Watch Site"]
    assert payload["sites"][0]["watch_status"] == "critical"
    assert payload["sites"][0]["top_alert"]
    assert payload["sites"][0]["recommended_action"]
    assert payload["sites"][1]["watch_status"] == "warning"


def test_api_client_workload_groups_open_actions_by_account(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Commerce",
            url="https://critical-commerce.example",
            client="Client Commerce",
            uptime_ok="false",
            ssl_days="4",
            wp_updates="6",
            backup_age_hours="96",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Commerce Warning",
            url="https://commerce-warning.example",
            client="Client Commerce",
            wp_updates="2",
            response_ms="900",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Unassigned Warning",
            url="https://unassigned-warning.example",
            client="",
            wp_updates="1",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/client-workload")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["client_count"] == 2
    assert payload["open_action_count"] >= 4
    assert payload["critical_action_count"] >= 1
    assert [row["client"] for row in payload["clients"]] == ["Client Commerce", "Unassigned"]
    commerce = payload["clients"][0]
    assert commerce["site_count"] == 2
    assert commerce["open_action_count"] >= 4
    assert commerce["critical_action_count"] >= 1
    assert commerce["warning_action_count"] >= 1
    assert commerce["lowest_score"] < 70
    assert commerce["top_site"] == "Critical Commerce"
    assert commerce["top_recommended_action"] == "Confirm site availability, hosting status, and recent deploys."
    assert commerce["latest_snapshot_at"]


def test_api_incidents_returns_only_critical_alerts(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Client Site",
            url="https://critical-client.example",
            client="Client Critical",
            uptime_ok="false",
            ssl_days="2",
            wp_updates="5",
            backup_age_hours="120",
            response_ms="2600",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Warning Only Site",
            url="https://warning-only.example",
            client="Client Warning",
            wp_updates="1",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/incidents")

    assert response.status_code == 200
    payload = response.json()
    assert payload["incident_count"] >= 1
    assert payload["affected_site_count"] == 1
    assert payload["affected_client_count"] == 1
    assert all(incident["severity"] == "critical" for incident in payload["incidents"])
    assert {incident["site"] for incident in payload["incidents"]} == {"Critical Client Site"}
    assert payload["incidents"][0]["client"] == "Client Critical"
    assert payload["incidents"][0]["recommended_action"]


def test_api_availability_fails_closed_for_missing_and_stale_evidence(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Down Store",
            url="https://down-store.example",
            client="Client Commerce",
            uptime_ok="false",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Healthy Store",
            url="https://healthy-store.example",
            client="Client Commerce",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Store",
            url="https://stale-store.example",
            client="Client Legacy",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Store",
            "url": "https://missing-store.example",
            "client": "Client New",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("2000-01-01 00:00:00", "https://stale-store.example"),
        )

    response = client.get("/api/availability")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["status"] == "red"
    assert payload["site_count"] == 4
    assert payload["current_evidence_count"] == 2
    assert payload["available_count"] == 1
    assert payload["down_count"] == 1
    assert payload["unknown_count"] == 2
    assert payload["availability_evidence_percent"] == 50
    assert [site["name"] for site in payload["sites"]] == [
        "Down Store",
        "Missing Store",
        "Stale Store",
        "Healthy Store",
    ]

    down, missing, stale, healthy = payload["sites"]
    assert down["availability_status"] == "down"
    assert down["snapshot_freshness"] == "current"
    assert down["reachable"] is False
    assert down["last_observed_reachable"] is False
    assert down["recommended_action"] == "Confirm site availability, hosting status, DNS, and recent deploys."
    assert missing["availability_status"] == "unknown"
    assert missing["snapshot_freshness"] == "missing"
    assert missing["reachable"] is None
    assert missing["last_observed_reachable"] is None
    assert missing["latest_snapshot_at"] is None
    assert stale["availability_status"] == "unknown"
    assert stale["snapshot_freshness"] == "stale"
    assert stale["reachable"] is None
    assert stale["last_observed_reachable"] is True
    assert stale["snapshot_age_hours"] > 168
    assert healthy["availability_status"] == "available"
    assert healthy["snapshot_freshness"] == "current"
    assert healthy["reachable"] is True


def test_api_backups_highlights_stale_backup_queue(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Backup",
            url="https://critical-backup.example",
            client="Client Backup",
            backup_age_hours="120",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Warning Backup",
            url="https://warning-backup.example",
            client="Client Backup",
            backup_age_hours="48",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Fresh Backup", url="https://fresh-backup.example", backup_age_hours="12"),
        follow_redirects=False,
    )

    response = client.get("/api/backups")

    assert response.status_code == 200
    payload = response.json()
    assert payload["site_count"] == 3
    assert payload["fresh_count"] == 1
    assert payload["stale_count"] == 2
    assert payload["oldest_backup_age_hours"] == 120
    assert [site["name"] for site in payload["sites"]] == ["Critical Backup", "Warning Backup", "Fresh Backup"]
    assert payload["sites"][0]["backup_status"] == "critical"
    assert payload["sites"][0]["recommended_action"] == "Run and verify an immediate backup."
    assert payload["sites"][1]["backup_status"] == "warning"
    assert payload["sites"][2]["backup_status"] == "fresh"


def test_api_backups_fails_closed_for_incomplete_snapshot_evidence(tmp_path):
    client = make_test_client(tmp_path)
    for name, url in (
        ("Current Backup", "https://current-backup.example"),
        ("Stale Backup", "https://stale-evidence-backup.example"),
        ("Invalid Backup", "https://invalid-evidence-backup.example"),
        ("Future Backup", "https://future-evidence-backup.example"),
    ):
        client.post(
            "/snapshot",
            data=valid_snapshot_payload(name=name, url=url, backup_age_hours="12"),
            follow_redirects=False,
        )
    client.post(
        "/sites",
        data={
            "name": "Missing Backup",
            "url": "https://missing-evidence-backup.example",
            "client": "Client New",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("2000-01-01 00:00:00", "https://stale-evidence-backup.example"),
        )
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("not-a-timestamp", "https://invalid-evidence-backup.example"),
        )
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), "https://future-evidence-backup.example"),
        )

    response = client.get("/api/backups")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "yellow"
    assert payload["site_count"] == 5
    assert payload["current_evidence_count"] == 1
    assert payload["fresh_count"] == 1
    assert payload["warning_count"] == 0
    assert payload["critical_count"] == 0
    assert payload["stale_count"] == 0
    assert payload["unknown_count"] == 4
    assert payload["backup_evidence_percent"] == 20
    assert payload["oldest_backup_age_hours"] == 12
    assert [site["name"] for site in payload["sites"]] == [
        "Missing Backup",
        "Invalid Backup",
        "Future Backup",
        "Stale Backup",
        "Current Backup",
    ]

    missing, invalid, future, stale, current = payload["sites"]
    assert missing["backup_status"] == "unknown"
    assert missing["snapshot_freshness"] == "missing"
    assert missing["backup_age_hours"] is None
    assert missing["last_observed_backup_age_hours"] is None
    assert missing["recommended_action"] == "Capture an initial fleet snapshot and verify backup freshness."
    assert invalid["snapshot_freshness"] == "invalid"
    assert future["snapshot_freshness"] == "clock_skew"
    assert stale["snapshot_freshness"] == "stale"
    assert stale["backup_status"] == "unknown"
    assert stale["backup_age_hours"] is None
    assert stale["last_observed_backup_age_hours"] == 12
    assert stale["snapshot_age_hours"] > 168
    assert stale["recommended_action"] == "Capture a fresh fleet snapshot before relying on backup status."
    assert current["backup_status"] == "fresh"
    assert current["snapshot_freshness"] == "current"
    assert current["backup_age_hours"] == 12
    assert current["last_observed_backup_age_hours"] == 12


def test_api_actions_surfaces_aging_backup_warning(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Backup Watch",
            url="https://backup-watch.example",
            client="Client Backup",
            backup_age_hours="48",
        ),
        follow_redirects=False,
    )

    payload = client.get("/api/actions").json()

    assert payload["action_count"] == 1
    assert payload["actions"][0]["site"] == "Backup Watch"
    assert payload["actions"][0]["severity"] == "warning"
    assert payload["actions"][0]["message"] == "Latest backup is 48 hours old."
    assert payload["actions"][0]["recommended_action"] == (
        "Run and verify a fresh backup, then confirm backup scheduling."
    )


def test_api_backup_remediation_groups_stale_backup_work_by_client(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Client Backup",
            url="https://critical-client-backup.example",
            client="Client Backup",
            backup_age_hours="144",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Warning Client Backup",
            url="https://warning-client-backup.example",
            client="Client Backup",
            backup_age_hours="48",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Fresh Client Backup",
            url="https://fresh-client-backup.example",
            client="Client Backup",
            backup_age_hours="12",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Unassigned Critical Backup",
            url="https://unassigned-critical-backup.example",
            client="",
            backup_age_hours="96",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/backup-remediation")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["client_count"] == 2
    assert payload["site_count"] == 4
    assert payload["stale_site_count"] == 3
    assert payload["critical_site_count"] == 2
    assert [client_row["client"] for client_row in payload["clients"]] == ["Client Backup", "Unassigned"]
    primary = payload["clients"][0]
    assert primary["site_count"] == 3
    assert primary["stale_site_count"] == 2
    assert primary["critical_site_count"] == 1
    assert primary["oldest_backup_age_hours"] == 144
    assert primary["backup_status"] == "critical"
    assert primary["recommended_action"] == "Run immediate backups for critical sites, then verify schedules for warning sites."
    assert [site["name"] for site in primary["sites"]] == ["Critical Client Backup", "Warning Client Backup"]
    assert payload["clients"][1]["client"] == "Unassigned"
    assert payload["clients"][1]["backup_status"] == "critical"


def test_api_backup_remediation_fails_closed_for_incomplete_snapshot_evidence(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Current Critical Backup",
            url="https://current-remediation-backup.example",
            client="Client Recovery",
            backup_age_hours="96",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Critical Backup",
            url="https://stale-remediation-backup.example",
            client="Client Recovery",
            backup_age_hours="120",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Backup Evidence",
            "url": "https://missing-remediation-backup.example",
            "client": "Client Recovery",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("2000-01-01 00:00:00", "https://stale-remediation-backup.example"),
        )

    response = client.get("/api/backup-remediation")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "red"
    assert payload["site_count"] == 3
    assert payload["current_evidence_count"] == 1
    assert payload["unknown_count"] == 2
    assert payload["backup_evidence_percent"] == 33
    assert payload["stale_site_count"] == 1
    assert payload["critical_site_count"] == 1

    account = payload["clients"][0]
    assert account["client"] == "Client Recovery"
    assert account["site_count"] == 3
    assert account["current_evidence_count"] == 1
    assert account["unknown_site_count"] == 2
    assert account["backup_evidence_percent"] == 33
    assert account["backup_status"] == "critical"
    assert [site["name"] for site in account["sites"]] == [
        "Current Critical Backup",
        "Missing Backup Evidence",
        "Stale Critical Backup",
    ]

    current, missing, stale = account["sites"]
    assert current["backup_status"] == "critical"
    assert current["snapshot_freshness"] == "current"
    assert current["backup_age_hours"] == 96
    assert current["last_observed_backup_age_hours"] == 96
    assert missing["backup_status"] == "unknown"
    assert missing["snapshot_freshness"] == "missing"
    assert missing["backup_age_hours"] is None
    assert missing["last_observed_backup_age_hours"] is None
    assert missing["recommended_action"] == (
        "Capture an initial fleet snapshot and verify backup freshness."
    )
    assert stale["backup_status"] == "unknown"
    assert stale["snapshot_freshness"] == "stale"
    assert stale["backup_age_hours"] is None
    assert stale["last_observed_backup_age_hours"] == 120
    assert stale["snapshot_age_hours"] > 168
    assert stale["recommended_action"] == (
        "Capture a fresh fleet snapshot before relying on backup status."
    )


def test_api_restore_drill_queue_prioritizes_backup_recovery_risk(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Overdue Restore Drill",
            url="https://overdue-restore.example",
            client="Client DR",
            backup_age_hours="240",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="High Restore Drill",
            url="https://high-restore.example",
            client="Client DR",
            backup_age_hours="96",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Watch Restore Drill",
            url="https://watch-restore.example",
            backup_age_hours="36",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Routine Restore Drill", url="https://routine-restore.example", backup_age_hours="12"),
        follow_redirects=False,
    )

    response = client.get("/api/restore-drill-queue")

    assert response.status_code == 200
    payload = response.json()
    assert payload["site_count"] == 4
    assert payload["urgent_count"] == 1
    assert payload["high_count"] == 1
    assert payload["watch_count"] == 1
    assert payload["routine_count"] == 1
    assert [site["name"] for site in payload["sites"]] == [
        "Overdue Restore Drill",
        "High Restore Drill",
        "Watch Restore Drill",
        "Routine Restore Drill",
    ]
    assert payload["sites"][0]["restore_drill_priority"] == "urgent"
    assert payload["sites"][0]["recommended_action"] == "Run an immediate restore drill and verify a recent usable backup exists."
    assert payload["sites"][1]["restore_drill_priority"] == "high"
    assert payload["sites"][2]["restore_drill_priority"] == "watch"
    assert payload["sites"][3]["restore_drill_priority"] == "routine"


def test_api_restore_drill_queue_fails_closed_for_incomplete_backup_evidence(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Current Urgent Restore Drill",
            url="https://current-urgent-restore.example",
            backup_age_hours="180",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Routine Restore Drill",
            url="https://stale-routine-restore.example",
            backup_age_hours="12",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Restore Evidence",
            "url": "https://missing-restore-evidence.example",
            "client": "Client New",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("2000-01-01 00:00:00", "https://stale-routine-restore.example"),
        )

    response = client.get("/api/restore-drill-queue")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "red"
    assert payload["site_count"] == 3
    assert payload["current_evidence_count"] == 1
    assert payload["unknown_count"] == 2
    assert payload["urgent_count"] == 1
    assert payload["high_count"] == 0
    assert payload["watch_count"] == 0
    assert payload["routine_count"] == 0
    assert payload["restore_evidence_percent"] == 33
    assert [site["name"] for site in payload["sites"]] == [
        "Missing Restore Evidence",
        "Stale Routine Restore Drill",
        "Current Urgent Restore Drill",
    ]

    missing, stale, current = payload["sites"]
    assert missing["restore_drill_priority"] == "unknown"
    assert missing["snapshot_freshness"] == "missing"
    assert missing["backup_age_hours"] is None
    assert missing["last_observed_backup_age_hours"] is None
    assert missing["recommended_action"] == (
        "Capture an initial fleet snapshot and verify backup restore readiness."
    )
    assert stale["restore_drill_priority"] == "unknown"
    assert stale["snapshot_freshness"] == "stale"
    assert stale["backup_age_hours"] is None
    assert stale["last_observed_backup_age_hours"] == 12
    assert stale["snapshot_age_hours"] > 168
    assert stale["recommended_action"] == (
        "Capture a fresh fleet snapshot before scheduling a restore drill."
    )
    assert current["restore_drill_priority"] == "urgent"
    assert current["snapshot_freshness"] == "current"
    assert current["backup_age_hours"] == 180
    assert current["last_observed_backup_age_hours"] == 180


def test_api_security_highlights_header_coverage_gaps(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Missing Headers",
            url="https://missing-headers.example",
            client="Client Security",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Partial Headers",
            url="https://partial-headers.example",
            client="Client Security",
            security_header_count="2",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Covered Headers",
            url="https://covered-headers.example",
            security_header_count="3",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/security")

    assert response.status_code == 200
    payload = response.json()
    assert payload["site_count"] == 3
    assert payload["covered_count"] == 1
    assert payload["gap_count"] == 2
    assert payload["average_security_header_count"] == 1.7
    assert [site["name"] for site in payload["sites"]] == ["Missing Headers", "Partial Headers", "Covered Headers"]
    assert payload["sites"][0]["security_status"] == "critical"
    assert payload["sites"][0]["recommended_action"] == "Add HSTS and clickjacking protection headers."
    assert payload["sites"][1]["security_status"] == "warning"
    assert payload["sites"][2]["security_status"] == "covered"


def test_api_security_fails_closed_for_incomplete_snapshot_evidence(tmp_path):
    client = make_test_client(tmp_path)
    for name, url in (
        ("Current Security", "https://current-security.example"),
        ("Stale Security", "https://stale-security.example"),
        ("Invalid Security", "https://invalid-security.example"),
        ("Future Security", "https://future-security.example"),
    ):
        client.post(
            "/snapshot",
            data=valid_snapshot_payload(name=name, url=url, security_header_count="3"),
            follow_redirects=False,
        )
    client.post(
        "/sites",
        data={
            "name": "Missing Security",
            "url": "https://missing-security.example",
            "client": "Client New",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("2000-01-01 00:00:00", "https://stale-security.example"),
        )
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("not-a-timestamp", "https://invalid-security.example"),
        )
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), "https://future-security.example"),
        )

    response = client.get("/api/security")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "yellow"
    assert payload["site_count"] == 5
    assert payload["current_evidence_count"] == 1
    assert payload["covered_count"] == 1
    assert payload["warning_count"] == 0
    assert payload["critical_count"] == 0
    assert payload["gap_count"] == 0
    assert payload["unknown_count"] == 4
    assert payload["security_evidence_percent"] == 20
    assert payload["average_security_header_count"] == 3
    assert [site["name"] for site in payload["sites"]] == [
        "Missing Security",
        "Invalid Security",
        "Future Security",
        "Stale Security",
        "Current Security",
    ]

    missing, invalid, future, stale, current = payload["sites"]
    assert missing["security_status"] == "unknown"
    assert missing["snapshot_freshness"] == "missing"
    assert missing["security_header_count"] is None
    assert missing["last_observed_security_header_count"] is None
    assert missing["recommended_action"] == (
        "Capture an initial fleet snapshot and verify security header coverage."
    )
    assert invalid["snapshot_freshness"] == "invalid"
    assert future["snapshot_freshness"] == "clock_skew"
    assert stale["snapshot_freshness"] == "stale"
    assert stale["security_status"] == "unknown"
    assert stale["security_header_count"] is None
    assert stale["last_observed_security_header_count"] == 3
    assert stale["snapshot_age_hours"] > 168
    assert stale["recommended_action"] == (
        "Capture a fresh fleet snapshot before relying on security coverage."
    )
    assert current["security_status"] == "covered"
    assert current["snapshot_freshness"] == "current"
    assert current["security_header_count"] == 3
    assert current["last_observed_security_header_count"] == 3


def test_api_performance_prioritizes_slowest_sites(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Slow Checkout",
            url="https://slow-checkout.example",
            client="Client Commerce",
            response_ms="2200",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Needs Cache",
            url="https://needs-cache.example",
            client="Client Content",
            response_ms="900",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Fast Site", url="https://fast.example", response_ms="250"),
        follow_redirects=False,
    )

    response = client.get("/api/performance")

    assert response.status_code == 200
    payload = response.json()
    assert payload["site_count"] == 3
    assert payload["slow_count"] == 1
    assert payload["warning_count"] == 1
    assert payload["average_response_ms"] == 1117
    assert payload["max_response_ms"] == 2200
    assert [site["name"] for site in payload["sites"]] == ["Slow Checkout", "Needs Cache", "Fast Site"]
    assert payload["sites"][0]["performance_status"] == "slow"
    assert payload["sites"][0]["recommended_action"] == "Investigate hosting, caching, and heavy checkout/page dependencies."
    assert payload["sites"][1]["performance_status"] == "warning"
    assert payload["sites"][2]["performance_status"] == "fast"


def test_api_performance_fails_closed_for_incomplete_snapshot_evidence(tmp_path):
    client = make_test_client(tmp_path)
    for name, url in (
        ("Current Performance", "https://current-performance.example"),
        ("Stale Performance", "https://stale-performance.example"),
        ("Invalid Performance", "https://invalid-performance.example"),
        ("Future Performance", "https://future-performance.example"),
    ):
        client.post(
            "/snapshot",
            data=valid_snapshot_payload(name=name, url=url, response_ms="250"),
            follow_redirects=False,
        )
    client.post(
        "/sites",
        data={
            "name": "Missing Performance",
            "url": "https://missing-performance.example",
            "client": "Client New",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("2000-01-01 00:00:00", "https://stale-performance.example"),
        )
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("not-a-timestamp", "https://invalid-performance.example"),
        )
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), "https://future-performance.example"),
        )

    response = client.get("/api/performance")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "yellow"
    assert payload["site_count"] == 5
    assert payload["current_evidence_count"] == 1
    assert payload["slow_count"] == 0
    assert payload["warning_count"] == 0
    assert payload["fast_count"] == 1
    assert payload["unknown_count"] == 4
    assert payload["performance_evidence_percent"] == 20
    assert payload["average_response_ms"] == 250
    assert payload["max_response_ms"] == 250
    assert [site["name"] for site in payload["sites"]] == [
        "Missing Performance",
        "Invalid Performance",
        "Future Performance",
        "Stale Performance",
        "Current Performance",
    ]

    missing, invalid, future, stale, current = payload["sites"]
    assert missing["performance_status"] == "unknown"
    assert missing["snapshot_freshness"] == "missing"
    assert missing["response_ms"] is None
    assert missing["last_observed_response_ms"] is None
    assert missing["recommended_action"] == (
        "Capture an initial fleet snapshot and verify response time."
    )
    assert invalid["snapshot_freshness"] == "invalid"
    assert future["snapshot_freshness"] == "clock_skew"
    assert stale["snapshot_freshness"] == "stale"
    assert stale["performance_status"] == "unknown"
    assert stale["response_ms"] is None
    assert stale["last_observed_response_ms"] == 250
    assert stale["snapshot_age_hours"] > 168
    assert stale["recommended_action"] == (
        "Capture a fresh fleet snapshot before relying on performance status."
    )
    assert current["performance_status"] == "fast"
    assert current["snapshot_freshness"] == "current"
    assert current["response_ms"] == 250
    assert current["last_observed_response_ms"] == 250


def test_api_certificates_prioritizes_expiring_tls_inventory(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Expired Cert",
            url="https://expired.example",
            client="Client TLS",
            ssl_days="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Renew Soon",
            url="https://renew-soon.example",
            client="Client TLS",
            ssl_days="12",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Healthy Cert", url="https://healthy-cert.example", ssl_days="61"),
        follow_redirects=False,
    )

    response = client.get("/api/certificates")

    assert response.status_code == 200
    payload = response.json()
    assert payload["site_count"] == 3
    assert payload["critical_count"] == 1
    assert payload["warning_count"] == 1
    assert payload["minimum_ssl_days"] == 0
    assert [site["name"] for site in payload["sites"]] == ["Expired Cert", "Renew Soon", "Healthy Cert"]
    assert payload["sites"][0]["certificate_status"] == "critical"
    assert payload["sites"][0]["recommended_action"] == "Renew or replace the TLS certificate immediately."
    assert payload["sites"][1]["certificate_status"] == "warning"
    assert payload["sites"][2]["certificate_status"] == "healthy"


def test_api_certificates_fails_closed_for_incomplete_snapshot_evidence(tmp_path):
    client = make_test_client(tmp_path)
    for name, url in (
        ("Current Certificate", "https://current-certificate.example"),
        ("Stale Certificate", "https://stale-certificate.example"),
        ("Invalid Certificate", "https://invalid-certificate.example"),
        ("Future Certificate", "https://future-certificate.example"),
    ):
        client.post(
            "/snapshot",
            data=valid_snapshot_payload(name=name, url=url, ssl_days="90"),
            follow_redirects=False,
        )
    client.post(
        "/sites",
        data={
            "name": "Missing Certificate",
            "url": "https://missing-certificate.example",
            "client": "Client New",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("2000-01-01 00:00:00", "https://stale-certificate.example"),
        )
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("not-a-timestamp", "https://invalid-certificate.example"),
        )
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), "https://future-certificate.example"),
        )

    response = client.get("/api/certificates")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "yellow"
    assert payload["site_count"] == 5
    assert payload["current_evidence_count"] == 1
    assert payload["healthy_count"] == 1
    assert payload["warning_count"] == 0
    assert payload["critical_count"] == 0
    assert payload["unknown_count"] == 4
    assert payload["certificate_evidence_percent"] == 20
    assert payload["minimum_ssl_days"] == 90
    assert [site["name"] for site in payload["sites"]] == [
        "Missing Certificate",
        "Invalid Certificate",
        "Future Certificate",
        "Stale Certificate",
        "Current Certificate",
    ]

    missing, invalid, future, stale, current = payload["sites"]
    assert missing["certificate_status"] == "unknown"
    assert missing["snapshot_freshness"] == "missing"
    assert missing["ssl_days_remaining"] is None
    assert missing["last_observed_ssl_days_remaining"] is None
    assert missing["recommended_action"] == (
        "Capture an initial fleet snapshot and verify certificate expiry."
    )
    assert invalid["snapshot_freshness"] == "invalid"
    assert future["snapshot_freshness"] == "clock_skew"
    assert stale["snapshot_freshness"] == "stale"
    assert stale["certificate_status"] == "unknown"
    assert stale["ssl_days_remaining"] is None
    assert stale["last_observed_ssl_days_remaining"] == 90
    assert stale["snapshot_age_hours"] > 168
    assert stale["recommended_action"] == (
        "Capture a fresh fleet snapshot before relying on certificate status."
    )
    assert current["certificate_status"] == "healthy"
    assert current["snapshot_freshness"] == "current"
    assert current["ssl_days_remaining"] == 90
    assert current["last_observed_ssl_days_remaining"] == 90


def test_api_actions_include_thirty_day_certificate_renewals(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Thirty Day Renewal",
            url="https://thirty-day-renewal.example",
            client="Client TLS",
            ssl_days="30",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/actions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["action_count"] == 1
    assert payload["actions"][0] == {
        "site": "Thirty Day Renewal",
        "url": "https://thirty-day-renewal.example",
        "client": "Client TLS",
        "score": 90,
        "severity": "warning",
        "message": "SSL expires in 30 day(s).",
        "recommended_action": "Renew or replace the TLS certificate before it expires.",
        "latest_snapshot_at": payload["actions"][0]["latest_snapshot_at"],
    }
    assert "Plan SSL renewal: 30 day(s) remaining." in client.get("/report").text


def test_api_certificate_renewal_calendar_groups_expiring_certificates(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Expired Cert",
            url="https://expired-renewal.example",
            client="Client TLS",
            ssl_days="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Immediate Cert",
            url="https://immediate-renewal.example",
            client="Client TLS",
            ssl_days="5",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Scheduled Cert",
            url="https://scheduled-renewal.example",
            client="Client TLS",
            ssl_days="21",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Healthy Cert", url="https://healthy-renewal.example", ssl_days="90"),
        follow_redirects=False,
    )

    response = client.get("/api/certificate-renewal-calendar")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["status"] == "red"
    assert payload["site_count"] == 4
    assert payload["current_evidence_count"] == 4
    assert payload["renewal_count"] == 3
    assert payload["overdue_count"] == 1
    assert payload["immediate_count"] == 1
    assert payload["scheduled_count"] == 1
    assert payload["unknown_count"] == 0
    assert payload["renewal_evidence_percent"] == 100
    assert [site["name"] for site in payload["sites"]] == ["Expired Cert", "Immediate Cert", "Scheduled Cert"]
    assert [site["renewal_window"] for site in payload["sites"]] == ["overdue", "immediate", "scheduled"]
    assert payload["sites"][0]["recommended_action"] == "Replace the expired certificate and verify HTTPS immediately."
    assert payload["sites"][1]["recommended_action"] == "Renew the certificate this week and confirm post-renewal expiry."
    assert payload["sites"][2]["recommended_action"] == "Schedule certificate renewal before the 7-day critical window."


def test_api_certificate_renewal_calendar_fails_closed_for_incomplete_evidence(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Current Scheduled Renewal",
            url="https://current-calendar-renewal.example",
            ssl_days="21",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Expired Renewal",
            url="https://stale-calendar-renewal.example",
            ssl_days="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Renewal Evidence",
            "url": "https://missing-calendar-renewal.example",
            "client": "Client New",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("2000-01-01 00:00:00", "https://stale-calendar-renewal.example"),
        )

    response = client.get("/api/certificate-renewal-calendar")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "yellow"
    assert payload["site_count"] == 3
    assert payload["current_evidence_count"] == 1
    assert payload["renewal_count"] == 1
    assert payload["overdue_count"] == 0
    assert payload["immediate_count"] == 0
    assert payload["scheduled_count"] == 1
    assert payload["unknown_count"] == 2
    assert payload["renewal_evidence_percent"] == 33
    assert [site["name"] for site in payload["sites"]] == [
        "Missing Renewal Evidence",
        "Stale Expired Renewal",
        "Current Scheduled Renewal",
    ]

    missing, stale, current = payload["sites"]
    assert missing["renewal_window"] == "unknown"
    assert missing["snapshot_freshness"] == "missing"
    assert missing["ssl_days_remaining"] is None
    assert missing["last_observed_ssl_days_remaining"] is None
    assert missing["recommended_action"] == (
        "Capture an initial fleet snapshot and verify certificate expiry."
    )
    assert stale["renewal_window"] == "unknown"
    assert stale["snapshot_freshness"] == "stale"
    assert stale["ssl_days_remaining"] is None
    assert stale["last_observed_ssl_days_remaining"] == 0
    assert stale["snapshot_age_hours"] > 168
    assert stale["recommended_action"] == (
        "Capture a fresh fleet snapshot before relying on certificate status."
    )
    assert current["renewal_window"] == "scheduled"
    assert current["snapshot_freshness"] == "current"
    assert current["ssl_days_remaining"] == 21
    assert current["last_observed_ssl_days_remaining"] == 21


def test_api_updates_prioritizes_wordpress_update_backlog(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Major Backlog",
            url="https://major-backlog.example",
            client="Client Updates",
            wp_updates="7",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Minor Backlog",
            url="https://minor-backlog.example",
            client="Client Updates",
            wp_updates="2",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Current Site", url="https://current.example", wp_updates="0"),
        follow_redirects=False,
    )

    response = client.get("/api/updates")

    assert response.status_code == 200
    payload = response.json()
    assert payload["site_count"] == 3
    assert payload["backlog_count"] == 2
    assert payload["total_pending_updates"] == 9
    assert payload["max_pending_updates"] == 7
    assert [site["name"] for site in payload["sites"]] == ["Major Backlog", "Minor Backlog", "Current Site"]
    assert payload["sites"][0]["update_status"] == "critical"
    assert payload["sites"][0]["recommended_action"] == "Plan a supervised update window and backup verification before applying updates."
    assert payload["sites"][1]["update_status"] == "warning"
    assert payload["sites"][2]["update_status"] == "current"


def test_api_updates_fails_closed_for_incomplete_snapshot_evidence(tmp_path):
    client = make_test_client(tmp_path)
    for name, url in (
        ("Current Updates", "https://current-updates.example"),
        ("Stale Updates", "https://stale-updates.example"),
        ("Invalid Updates", "https://invalid-updates.example"),
        ("Future Updates", "https://future-updates.example"),
    ):
        client.post(
            "/snapshot",
            data=valid_snapshot_payload(name=name, url=url, wp_updates="3"),
            follow_redirects=False,
        )
    client.post(
        "/sites",
        data={
            "name": "Missing Updates",
            "url": "https://missing-updates.example",
            "client": "Client New",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("2000-01-01 00:00:00", "https://stale-updates.example"),
        )
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("not-a-timestamp", "https://invalid-updates.example"),
        )
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), "https://future-updates.example"),
        )

    response = client.get("/api/updates")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "yellow"
    assert payload["site_count"] == 5
    assert payload["current_evidence_count"] == 1
    assert payload["critical_count"] == 0
    assert payload["warning_count"] == 1
    assert payload["current_count"] == 0
    assert payload["unknown_count"] == 4
    assert payload["update_evidence_percent"] == 20
    assert payload["backlog_count"] == 1
    assert payload["total_pending_updates"] == 3
    assert payload["max_pending_updates"] == 3
    assert [site["name"] for site in payload["sites"]] == [
        "Missing Updates",
        "Invalid Updates",
        "Future Updates",
        "Stale Updates",
        "Current Updates",
    ]

    missing, invalid, future, stale, current = payload["sites"]
    assert missing["update_status"] == "unknown"
    assert missing["snapshot_freshness"] == "missing"
    assert missing["pending_updates"] is None
    assert missing["last_observed_pending_updates"] is None
    assert missing["recommended_action"] == (
        "Capture an initial fleet snapshot and verify the WordPress update backlog."
    )
    assert invalid["snapshot_freshness"] == "invalid"
    assert future["snapshot_freshness"] == "clock_skew"
    assert stale["snapshot_freshness"] == "stale"
    assert stale["update_status"] == "unknown"
    assert stale["pending_updates"] is None
    assert stale["last_observed_pending_updates"] == 3
    assert stale["snapshot_age_hours"] > 168
    assert stale["recommended_action"] == (
        "Capture a fresh fleet snapshot before relying on update status."
    )
    assert current["update_status"] == "warning"
    assert current["snapshot_freshness"] == "current"
    assert current["pending_updates"] == 3
    assert current["last_observed_pending_updates"] == 3


def test_api_risk_register_groups_current_risks_by_category(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Risk",
            url="https://critical-risk.example",
            client="Client Risk",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="7",
            backup_age_hours="120",
            response_ms="2600",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Warning Risk",
            url="https://warning-risk.example",
            client="Client Risk",
            ssl_days="24",
            wp_updates="1",
            backup_age_hours="48",
            response_ms="900",
            security_header_count="2",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/risk-register")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["category_count"] == 6
    assert payload["critical_category_count"] == 6
    entries = {entry["category"]: entry for entry in payload["entries"]}
    assert entries["availability"]["affected_site_count"] == 1
    assert entries["availability"]["highest_severity"] == "critical"
    assert entries["availability"]["sites"][0]["name"] == "Critical Risk"
    assert entries["tls"]["affected_site_count"] == 2
    assert entries["tls"]["sites"][0]["severity"] == "critical"
    assert entries["tls"]["sites"][1]["severity"] == "warning"
    assert payload["status"] == "red"
    assert payload["site_count"] == 2
    assert payload["monitored_site_count"] == 2
    assert payload["current_evidence_count"] == 2
    assert payload["missing_snapshot_count"] == 0
    assert payload["stale_snapshot_count"] == 0
    assert payload["unknown_count"] == 0
    assert payload["risk_evidence_percent"] == 100
    assert entries["updates"]["sites"][0]["recommended_action"].startswith("Apply WordPress")
    assert entries["security"]["sites"][0]["score"] < entries["security"]["sites"][1]["score"]


def test_api_risk_register_fails_closed_on_missing_or_stale_evidence(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Expired Risk Evidence",
            url="https://expired-risk-evidence.example",
            client="Client Risk Evidence",
            uptime_ok="false",
            ssl_days="2",
            wp_updates="8",
            backup_age_hours="120",
            response_ms="2600",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Risk Evidence",
            "url": "https://missing-risk-evidence.example",
            "client": "Client Risk Evidence",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    response = client.get("/api/risk-register")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "yellow"
    assert payload["snapshot_freshness_threshold_hours"] == 168
    assert payload["site_count"] == 2
    assert payload["monitored_site_count"] == 1
    assert payload["current_evidence_count"] == 0
    assert payload["missing_snapshot_count"] == 1
    assert payload["stale_snapshot_count"] == 1
    assert payload["unknown_count"] == 2
    assert payload["risk_evidence_percent"] == 0
    assert payload["category_count"] == 0
    assert payload["critical_category_count"] == 0
    assert payload["entries"] == []


def test_api_maintenance_windows_prioritizes_sites_needing_safe_work_windows(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Emergency Updates",
            url="https://emergency-updates.example",
            client="Client Work",
            uptime_ok="false",
            ssl_days="2",
            wp_updates="8",
            backup_age_hours="120",
            response_ms="2600",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Routine Updates",
            url="https://routine-updates.example",
            client="Client Work",
            ssl_days="24",
            wp_updates="2",
            backup_age_hours="48",
            response_ms="900",
            security_header_count="2",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Steady Site", url="https://steady.example"),
        follow_redirects=False,
    )

    response = client.get("/api/maintenance-windows")

    assert response.status_code == 200
    payload = response.json()
    assert payload["site_count"] == 3
    assert payload["window_count"] == 2
    assert payload["immediate_count"] == 1
    assert [site["name"] for site in payload["sites"]] == ["Emergency Updates", "Routine Updates"]
    assert payload["sites"][0]["maintenance_window"] == "immediate"
    assert payload["sites"][0]["risk_count"] >= payload["sites"][1]["risk_count"]
    assert "Take a verified backup" in payload["sites"][0]["recommended_action"]
    assert payload["sites"][1]["maintenance_window"] == "scheduled"
    assert "Plan a routine maintenance window" in payload["sites"][1]["recommended_action"]


def test_api_maintenance_calendar_groups_work_by_window(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Emergency Storefront",
            url="https://emergency.example",
            client="Client Emergency",
            uptime_ok="false",
            ssl_days="5",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2600",
            security_header_count="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Routine Blog",
            url="https://routine.example",
            client="Client Routine",
            wp_updates="2",
            response_ms="900",
            security_header_count="2",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Healthy Site", url="https://healthy-calendar.example", client="Client Healthy"),
        follow_redirects=False,
    )

    response = client.get("/api/maintenance-calendar")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["site_count"] == 3
    assert payload["window_count"] == 2
    assert [window["window"] for window in payload["windows"]] == ["immediate", "scheduled"]
    immediate = payload["windows"][0]
    assert immediate["label"] == "Immediate maintenance"
    assert immediate["site_count"] == 1
    assert immediate["client_count"] == 1
    assert immediate["total_risk_count"] >= 5
    assert immediate["top_site"] == "Emergency Storefront"
    assert immediate["recommended_action"].startswith("Take a verified backup")
    assert immediate["sites"][0]["reasons"][0] == "site availability incident"
    scheduled = payload["windows"][1]
    assert scheduled["label"] == "Scheduled maintenance"
    assert scheduled["site_count"] == 1
    assert scheduled["top_site"] == "Routine Blog"
    assert scheduled["recommended_action"].startswith("Plan a routine maintenance window")


def test_maintenance_views_fail_closed_on_missing_or_stale_evidence(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Current Routine Work",
            url="https://current-maintenance.example",
            wp_updates="2",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Expired Emergency Evidence",
            url="https://expired-maintenance.example",
            uptime_ok="false",
            ssl_days="2",
            wp_updates="8",
            backup_age_hours="120",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Maintenance Evidence",
            "url": "https://missing-maintenance.example",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update snapshots set captured_at = ? where site_id = (select id from sites where url = ?)",
            ("2000-01-01 00:00:00", "https://expired-maintenance.example"),
        )

    windows = client.get("/api/maintenance-windows").json()
    calendar = client.get("/api/maintenance-calendar").json()

    for payload in (windows, calendar):
        assert payload["status"] == "yellow"
        assert payload["snapshot_freshness_threshold_hours"] == 168
        assert payload["site_count"] == 3
        assert payload["monitored_site_count"] == 2
        assert payload["current_evidence_count"] == 1
        assert payload["missing_snapshot_count"] == 1
        assert payload["stale_snapshot_count"] == 1
        assert payload["unknown_count"] == 2
        assert payload["maintenance_evidence_percent"] == 33
        assert payload["window_count"] == 1

    assert windows["immediate_count"] == 0
    assert windows["scheduled_count"] == 1
    assert [site["name"] for site in windows["sites"]] == ["Current Routine Work"]
    assert calendar["immediate_site_count"] == 0
    assert calendar["scheduled_site_count"] == 1
    assert [window["window"] for window in calendar["windows"]] == ["scheduled"]
    assert calendar["windows"][0]["sites"][0]["name"] == "Current Routine Work"


def test_api_slo_returns_service_objective_compliance(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Healthy SLO", url="https://healthy-slo.example"),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Risky SLO",
            url="https://risky-slo.example",
            uptime_ok="false",
            ssl_days="5",
            backup_age_hours="96",
            response_ms="2200",
            security_header_count="1",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/slo")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["site_count"] == 2
    assert payload["objective_count"] == 6
    assert payload["at_risk_count"] == 5
    assert payload["worst_objective"]["compliance_percent"] == 50.0
    objectives = {objective["name"]: objective for objective in payload["objectives"]}
    assert objectives["availability"] == {
        "name": "availability",
        "label": "Sites reachable",
        "threshold": "site reachable",
        "met_count": 1,
        "miss_count": 1,
        "compliance_percent": 50.0,
        "status": "at_risk",
    }
    assert objectives["tls"]["threshold"] == ">= 14 days remaining"
    assert objectives["backups"]["threshold"] == "<= 72 hours old"
    assert objectives["performance"]["threshold"] == "<= 1500 ms"
    assert objectives["security"]["threshold"] == ">= 2 core headers"
    assert objectives["monitoring"] == {
        "name": "monitoring",
        "label": "Current monitoring evidence",
        "threshold": "snapshot <= 168 hours old",
        "met_count": 2,
        "miss_count": 0,
        "compliance_percent": 100.0,
        "status": "healthy",
    }


def test_api_slo_fails_closed_when_monitoring_evidence_is_missing_or_stale(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale SLO Evidence",
            url="https://stale-slo-evidence.example",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={"name": "Missing SLO Evidence", "url": "https://missing-slo-evidence.example"},
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    payload = client.get("/api/slo").json()

    assert payload["site_count"] == 2
    assert payload["monitored_site_count"] == 1
    assert payload["current_snapshot_count"] == 0
    assert payload["at_risk_count"] == 6
    objectives = {objective["name"]: objective for objective in payload["objectives"]}
    assert set(objectives) == {"availability", "tls", "backups", "performance", "security", "monitoring"}
    for objective in objectives.values():
        assert objective["met_count"] == 0
        assert objective["miss_count"] == 2
        assert objective["compliance_percent"] == 0.0
        assert objective["status"] == "at_risk"
    assert payload["worst_objective"] == objectives["availability"]


def test_api_remediation_plan_groups_actions_by_operational_timing(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Immediate Fix",
            url="https://immediate-fix.example",
            client="Client Immediate",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="120",
            response_ms="2600",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Scheduled Fix",
            url="https://scheduled-fix.example",
            client="Client Scheduled",
            wp_updates="2",
            response_ms="900",
            security_header_count="2",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/remediation-plan")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["action_count"] >= 5
    assert payload["immediate_count"] >= 1
    assert payload["scheduled_count"] >= 1
    assert payload["watch_count"] >= 1
    assert [bucket["bucket"] for bucket in payload["buckets"]] == ["immediate", "scheduled", "watch"]
    immediate = payload["buckets"][0]
    scheduled = payload["buckets"][1]
    assert immediate["label"] == "Immediate remediation"
    assert immediate["actions"][0]["site"] == "Immediate Fix"
    assert immediate["actions"][0]["due"] == "today"
    assert scheduled["label"] == "Scheduled maintenance"
    assert scheduled["actions"][0]["due"] == "next maintenance window"


def test_api_client_digest_returns_account_checkin_summaries(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Client Site",
            url="https://critical-client.example",
            client="Client Critical",
            uptime_ok="false",
            ssl_days="4",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Healthy Client Site",
            url="https://healthy-client.example",
            client="Client Healthy",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/client-digest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["client_count"] == 2
    assert payload["red_count"] == 1
    assert payload["green_count"] == 1
    assert [row["client"] for row in payload["clients"]] == ["Client Critical", "Client Healthy"]
    critical = payload["clients"][0]
    assert critical["status"] == "red"
    assert critical["site_count"] == 1
    assert critical["immediate_action_count"] >= 1
    assert critical["scheduled_action_count"] >= 1
    assert critical["open_action_count"] >= 5
    assert critical["top_site"] == "Critical Client Site"
    assert "Client Critical has 1 tracked site" in critical["executive_summary"]
    assert critical["sites"][0]["critical_alerts"] >= 1
    healthy = payload["clients"][1]
    assert healthy["status"] == "green"
    assert healthy["open_action_count"] == 0
    assert healthy["top_message"] == "No open fleet actions."


def test_api_client_digest_warns_about_account_monitoring_gaps(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Digest Site",
            url="https://stale-digest.example",
            client="Client Digest Gap",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Digest Site",
            "url": "https://missing-digest.example",
            "client": "Client Digest Gap",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    payload = client.get("/api/client-digest").json()

    assert payload["client_count"] == 1
    assert payload["yellow_count"] == 1
    assert payload["missing_snapshot_count"] == 1
    assert payload["stale_snapshot_count"] == 1
    digest = payload["clients"][0]
    assert digest["client"] == "Client Digest Gap"
    assert digest["status"] == "yellow"
    assert digest["site_count"] == 2
    assert digest["monitored_site_count"] == 1
    assert digest["missing_snapshot_count"] == 1
    assert digest["current_snapshot_count"] == 0
    assert digest["stale_snapshot_count"] == 1
    assert digest["monitoring_coverage_percent"] == 50
    assert digest["snapshot_freshness_percent"] == 0
    assert digest["average_score"] == 100
    assert digest["open_action_count"] == 0
    assert [site["snapshot_freshness"] for site in digest["sites"]] == ["missing", "stale"]
    stale_site = next(site for site in digest["sites"] if site["snapshot_freshness"] == "stale")
    assert stale_site["score"] is None
    assert stale_site["status"] == "unknown"
    assert stale_site["critical_alerts"] == 0
    assert stale_site["observed_score"] < 65
    assert stale_site["observed_status"] == "red"
    assert stale_site["observed_critical_alerts"] >= 1
    assert "1 of 2 tracked sites have snapshots" in digest["executive_summary"]


def test_api_client_escalations_groups_critical_incidents_by_client(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Storefront",
            url="https://critical-storefront.example",
            client="Client Escalate",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Second Critical",
            url="https://second-critical.example",
            client="Client Escalate",
            uptime_ok="false",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Warning Only",
            url="https://warning-only-escalation.example",
            client="Client Warning",
            wp_updates="1",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/client-escalations")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["client_count"] == 1
    assert payload["affected_site_count"] == 2
    assert payload["critical_incident_count"] >= 4
    escalation = payload["clients"][0]
    assert escalation["client"] == "Client Escalate"
    assert escalation["affected_site_count"] == 2
    assert escalation["critical_incident_count"] >= 4
    assert escalation["lowest_score"] < 70
    assert escalation["top_site"] == "Critical Storefront"
    assert escalation["top_recommended_action"]
    assert all(incident["severity"] == "critical" for incident in escalation["incidents"])
    assert {incident["site"] for incident in escalation["incidents"]} == {"Critical Storefront", "Second Critical"}


def test_api_stale_snapshots_flags_missing_and_old_snapshots(tmp_path):
    client = make_test_client(tmp_path)
    db_path = tmp_path / "test.sqlite3"
    client.post("/sites", data={"name": "No Snapshot", "url": "https://missing.example", "client": "Client Missing"}, follow_redirects=False)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Fresh Snapshot", url="https://fresh-snapshot.example", client="Client Fresh"),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Old Snapshot", url="https://old-snapshot.example", client="Client Old"),
        follow_redirects=False,
    )
    old_captured_at = (datetime.now(timezone.utc) - timedelta(hours=240)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db_path) as con:
        con.execute(
            "update snapshots set captured_at=? where site_id=(select id from sites where url=?)",
            (old_captured_at, "https://old-snapshot.example"),
        )

    response = client.get("/api/stale-snapshots?threshold_hours=168")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["threshold_hours"] == 168
    assert payload["site_count"] == 3
    assert payload["stale_count"] == 2
    assert payload["missing_snapshot_count"] == 1
    assert payload["current_snapshot_count"] == 1
    assert payload["snapshot_coverage_percent"] == 33
    assert [site["name"] for site in payload["sites"]] == ["No Snapshot", "Old Snapshot"]
    missing = payload["sites"][0]
    assert missing["client"] == "Client Missing"
    assert missing["staleness_status"] == "missing"
    assert missing["snapshot_age_hours"] is None
    old = payload["sites"][1]
    assert old["staleness_status"] == "stale"
    assert old["snapshot_age_hours"] >= 239
    assert old["recommended_action"] == "Capture a fresh fleet snapshot and verify site health."


def test_api_stale_snapshots_clamps_non_positive_threshold(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Fresh Snapshot", url="https://fresh-threshold.example"),
        follow_redirects=False,
    )

    payload = client.get("/api/stale-snapshots?threshold_hours=0").json()

    assert payload["threshold_hours"] == 1
    assert payload["stale_count"] == 0
    assert payload["current_snapshot_count"] == 1
    assert payload["snapshot_coverage_percent"] == 100


def test_api_stale_snapshots_flags_future_timestamps_as_clock_skew(tmp_path):
    client = make_test_client(tmp_path)
    db_path = tmp_path / "test.sqlite3"
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Future Snapshot", url="https://future-snapshot.example", client="Client Future"),
        follow_redirects=False,
    )
    future_captured_at = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db_path) as con:
        con.execute("update snapshots set captured_at=?", (future_captured_at,))

    payload = client.get("/api/stale-snapshots").json()

    assert payload["stale_count"] == 1
    assert payload["current_snapshot_count"] == 0
    assert payload["clock_skew_count"] == 1
    assert payload["snapshot_coverage_percent"] == 0
    site = payload["sites"][0]
    assert site["name"] == "Future Snapshot"
    assert site["staleness_status"] == "clock_skew"
    assert site["snapshot_age_hours"] <= -23
    assert site["recommended_action"] == "Correct the snapshot timestamp or source clock, then capture a fresh snapshot."


def test_api_stale_snapshots_flags_invalid_timestamps_for_repair(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Invalid Snapshot", url="https://invalid-snapshot.example", client="Client Invalid"),
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at=?", ("not-a-timestamp",))

    payload = client.get("/api/stale-snapshots").json()

    assert payload["stale_count"] == 1
    assert payload["current_snapshot_count"] == 0
    assert payload["invalid_timestamp_count"] == 1
    assert payload["snapshot_coverage_percent"] == 0
    site = payload["sites"][0]
    assert site["name"] == "Invalid Snapshot"
    assert site["staleness_status"] == "invalid"
    assert site["snapshot_age_hours"] is None
    assert site["recommended_action"] == "Repair the invalid snapshot timestamp, then capture a fresh snapshot."


def test_api_executive_risks_summarizes_client_risk_levels(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Executive Site",
            url="https://critical-executive.example",
            client="Client Critical",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Elevated Executive Site",
            url="https://elevated-executive.example",
            client="Client Elevated",
            wp_updates="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stable Executive Site",
            url="https://stable-executive.example",
            client="Client Stable",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/executive-risks")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["client_count"] == 3
    assert payload["critical_client_count"] == 1
    assert payload["elevated_client_count"] == 1
    assert payload["stable_client_count"] == 1
    assert [client_row["client"] for client_row in payload["clients"]] == [
        "Client Critical",
        "Client Elevated",
        "Client Stable",
    ]
    critical = payload["clients"][0]
    assert critical["risk_level"] == "critical"
    assert critical["critical_action_count"] >= 1
    assert critical["critical_site_count"] == 1
    assert critical["lowest_score"] < 70
    elevated = payload["clients"][1]
    assert elevated["risk_level"] == "elevated"
    assert elevated["warning_action_count"] >= 1
    stable = payload["clients"][2]
    assert stable["risk_level"] == "stable"
    assert stable["open_action_count"] == 0
    assert stable["average_score"] >= 85


def test_executive_handoffs_surface_monitoring_gaps_without_escalating_stale_risk(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Executive Site",
            url="https://stale-executive-gap.example",
            client="Client Executive Gap",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Executive Site",
            "url": "https://missing-executive-gap.example",
            "client": "Client Executive Gap",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    risks = client.get("/api/executive-risks").json()

    assert risks["client_count"] == 1
    assert risks["critical_client_count"] == 0
    assert risks["elevated_client_count"] == 1
    assert risks["stable_client_count"] == 0
    risk = risks["clients"][0]
    assert risk["client"] == "Client Executive Gap"
    assert risk["risk_level"] == "elevated"
    assert risk["site_count"] == 2
    assert risk["monitored_site_count"] == 1
    assert risk["current_snapshot_count"] == 0
    assert risk["missing_snapshot_count"] == 1
    assert risk["stale_snapshot_count"] == 1
    assert risk["monitoring_gap_count"] == 2
    assert risk["lowest_score"] == 100
    assert risk["critical_site_count"] == 0

    fleet_brief = client.get("/api/fleet-brief").json()

    assert fleet_brief["status"] == "yellow"
    assert fleet_brief["site_count"] == 2
    assert fleet_brief["client_count"] == 1
    assert fleet_brief["open_action_count"] == 0
    assert fleet_brief["missing_snapshot_count"] == 1
    assert fleet_brief["stale_snapshot_count"] == 1
    assert fleet_brief["monitoring_gap_count"] == 2
    assert fleet_brief["top_clients"][0]["client"] == "Client Executive Gap"

    handoff = client.get("/api/operator-handoff").json()

    assert handoff["status"] == "yellow"
    assert handoff["site_count"] == 2
    assert handoff["client_count"] == 1
    assert handoff["open_action_count"] == 0
    assert handoff["monitoring_gap_count"] == 2
    assert handoff["headline"] == "Yellow: 2 monitoring gaps require operator follow-up."
    assert handoff["top_clients"][0]["client"] == "Client Executive Gap"
    assert handoff["handoff_notes"][0] == (
        "Restore monitoring coverage for Client Executive Gap: 1 missing snapshot and 1 stale snapshot."
    )


def test_api_fleet_brief_returns_operator_summary(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Brief Site",
            url="https://critical-brief.example",
            client="Client Brief Critical",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stable Brief Site",
            url="https://stable-brief.example",
            client="Client Brief Stable",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/fleet-brief")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["status"] == "red"
    assert payload["site_count"] == 2
    assert payload["client_count"] == 2
    assert payload["critical_client_count"] == 1
    assert payload["immediate_action_count"] >= 1
    assert payload["open_action_count"] >= payload["immediate_action_count"]
    assert payload["at_risk_objective_count"] >= 1
    assert payload["worst_objective"]["name"] in {"availability", "tls", "backups", "performance", "security"}
    assert payload["top_clients"][0]["client"] == "Client Brief Critical"
    assert payload["top_clients"][0]["risk_level"] == "critical"
    assert payload["top_actions"][0]["site"] == "Critical Brief Site"


def test_api_site_scorecards_returns_compact_per_site_status_cards(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Scorecard",
            url="https://critical-scorecard.example",
            client="Client Scorecard",
            uptime_ok="false",
            ssl_days="4",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Healthy Scorecard",
            url="https://healthy-scorecard.example",
            client="Client Scorecard",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/site-scorecards")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["site_count"] == 2
    assert payload["critical_count"] == 1
    assert payload["warning_count"] == 0
    assert payload["healthy_count"] == 1
    assert [site["name"] for site in payload["sites"]] == ["Critical Scorecard", "Healthy Scorecard"]
    critical = payload["sites"][0]
    assert critical["status"] == "critical"
    assert critical["next_action"] == "Confirm site availability, hosting status, and recent deploys."
    assert critical["badges"] == {
        "availability": "critical",
        "tls": "critical",
        "updates": "critical",
        "backups": "critical",
        "performance": "slow",
        "security": "critical",
    }
    assert critical["alert_count"] >= 5
    assert payload["sites"][1]["status"] == "healthy"
    assert payload["sites"][1]["next_action"] == "Continue normal maintenance cadence."


def test_api_site_scorecards_fail_closed_on_stale_snapshot_evidence(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Scorecard",
            url="https://stale-scorecard.example",
            client="Client Scorecard Gap",
            uptime_ok="false",
            ssl_days="4",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    payload = client.get("/api/site-scorecards").json()

    assert payload["site_count"] == 1
    assert payload["critical_count"] == 0
    assert payload["warning_count"] == 0
    assert payload["healthy_count"] == 0
    assert payload["unknown_count"] == 1
    scorecard = payload["sites"][0]
    assert scorecard["status"] == "unknown"
    assert scorecard["observed_status"] == "critical"
    assert scorecard["score"] is None
    assert scorecard["observed_score"] < 65
    assert scorecard["snapshot_freshness"] == "stale"
    assert scorecard["snapshot_age_hours"] > 168
    assert set(scorecard["badges"].values()) == {"unknown"}
    assert scorecard["observed_badges"]["availability"] == "critical"
    assert scorecard["observed_alert_count"] >= 5
    assert scorecard["alert_count"] == 0
    assert scorecard["next_action"] == "Capture a fresh fleet snapshot before relying on this scorecard."


def test_api_snapshot_history_returns_recent_snapshots_newest_first(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="History Site", url="https://history.example", client="Client History", response_ms="200"),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="History Site",
            url="https://history.example",
            client="Client History",
            uptime_ok="false",
            ssl_days="2",
            wp_updates="7",
            backup_age_hours="120",
            response_ms="2600",
            security_header_count="0",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/snapshot-history?limit=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["limit"] == 1
    assert payload["snapshot_count"] == 1
    assert payload["previous_offset"] is None
    assert payload["next_offset"] == 1
    latest = payload["snapshots"][0]
    assert isinstance(latest["snapshot_id"], int)
    assert latest["name"] == "History Site"
    assert latest["client"] == "Client History"
    assert latest["status"] == "red"
    assert latest["uptime_ok"] is False
    assert latest["alert_count"] >= 5
    assert latest["alerts"][0]["severity"] == "critical"


def test_api_snapshot_history_supports_pagination(tmp_path):
    client = make_test_client(tmp_path)
    for i in range(4):
        client.post(
            "/snapshot",
            data=valid_snapshot_payload(
                name=f"History Site {i}",
                url=f"https://history-{i}.example",
                response_ms=str(200 + i * 100),
            ),
            follow_redirects=False,
        )

    response = client.get("/api/snapshot-history?limit=2&offset=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["limit"] == 2
    assert payload["offset"] == 1
    assert payload["snapshot_count"] == 2
    assert payload["total_snapshot_count"] == 4
    assert payload["has_more"] is True
    assert payload["range_start"] == 2
    assert payload["range_end"] == 3
    assert payload["remaining_count"] == 1
    assert payload["previous_offset"] == 0
    assert payload["next_offset"] == 3
    assert payload["snapshots"][0]["snapshot_id"] > payload["snapshots"][1]["snapshot_id"]
    assert [snapshot["response_ms"] for snapshot in payload["snapshots"]] == [400, 300]


@pytest.mark.parametrize(
    ("path", "count_key", "rows_key"),
    [
        ("/api/snapshot-history", "snapshot_count", "snapshots"),
        ("/api/care-check-history", "care_check_count", "care_checks"),
        ("/api/site-snapshot-history?url=https://bounded-history.example", "snapshot_count", "snapshots"),
    ],
)
def test_history_apis_clamp_offsets_past_the_last_page(tmp_path, path, count_key, rows_key):
    client = make_test_client(tmp_path)
    for response_ms in (200, 300, 400):
        client.post(
            "/snapshot",
            data=valid_snapshot_payload(
                name="Bounded History",
                url="https://bounded-history.example",
                response_ms=str(response_ms),
            ),
            follow_redirects=False,
        )

    separator = "&" if "?" in path else "?"
    payload = client.get(f"{path}{separator}limit=2&offset=999").json()

    assert payload["limit"] == 2
    assert payload["offset"] == 2
    assert payload[count_key] == 1
    assert payload["page_number"] == 2
    assert payload["page_count"] == 2
    assert payload["range_start"] == 3
    assert payload["range_end"] == 3
    assert payload["remaining_count"] == 0
    assert payload["first_offset"] == 0
    assert payload["last_offset"] == 2
    assert payload["previous_offset"] == 0
    assert payload["next_offset"] is None
    assert len(payload[rows_key]) == 1


@pytest.mark.parametrize(
    ("path", "count_key", "rows_key"),
    [
        ("/api/snapshot-history", "snapshot_count", "snapshots"),
        ("/api/care-check-history", "care_check_count", "care_checks"),
        ("/api/site-snapshot-history?url=https://empty-history.example", "snapshot_count", "snapshots"),
    ],
)
def test_history_apis_reset_offsets_when_filtered_history_is_empty(tmp_path, path, count_key, rows_key):
    client = make_test_client(tmp_path)

    separator = "&" if "?" in path else "?"
    payload = client.get(f"{path}{separator}limit=2&offset=999").json()

    assert payload["offset"] == 0
    assert payload[count_key] == 0
    assert payload["page_number"] == 0
    assert payload["page_count"] == 0
    assert payload["range_start"] is None
    assert payload["range_end"] is None
    assert payload["remaining_count"] == 0
    assert payload["first_offset"] is None
    assert payload["last_offset"] is None
    assert payload["previous_offset"] is None
    assert payload["next_offset"] is None
    assert payload[rows_key] == []


def test_api_snapshot_history_filters_by_normalized_site_url(tmp_path):
    client = make_test_client(tmp_path)
    for response_ms in (200, 500, 800):
        client.post(
            "/snapshot",
            data=valid_snapshot_payload(
                name="Filtered History Site",
                url="https://filtered-history.example",
                response_ms=str(response_ms),
            ),
            follow_redirects=False,
        )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Other History Site", url="https://other-history.example"),
        follow_redirects=False,
    )

    response = client.get(
        "/api/snapshot-history",
        params={"url": "HTTPS://FILTERED-HISTORY.EXAMPLE:443/#snapshots", "limit": 2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["url"] == "https://filtered-history.example"
    assert payload["snapshot_count"] == 2
    assert payload["total_snapshot_count"] == 3
    assert payload["has_more"] is True
    assert [snapshot["response_ms"] for snapshot in payload["snapshots"]] == [800, 500]
    assert all(snapshot["url"] == payload["url"] for snapshot in payload["snapshots"])


def test_api_snapshot_history_filters_and_paginates_by_client(tmp_path):
    client = make_test_client(tmp_path)
    for i in range(3):
        client.post(
            "/snapshot",
            data=valid_snapshot_payload(
                name=f"Alpha History {i}",
                url=f"https://alpha-history-{i}.example",
                client="Client Alpha",
                response_ms=str(300 + i * 100),
            ),
            follow_redirects=False,
        )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Beta History",
            url="https://beta-history.example",
            client="Client Beta",
            response_ms="900",
        ),
        follow_redirects=False,
    )

    response = client.get(
        "/api/snapshot-history",
        params={"client": "  Client Alpha  ", "limit": 2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["url"] is None
    assert payload["client"] == "Client Alpha"
    assert payload["snapshot_count"] == 2
    assert payload["total_snapshot_count"] == 3
    assert payload["has_more"] is True
    assert [snapshot["response_ms"] for snapshot in payload["snapshots"]] == [500, 400]
    assert all(snapshot["client"] == payload["client"] for snapshot in payload["snapshots"])


def test_api_site_trends_compares_latest_snapshot_to_previous(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Trend Regressing", url="https://trend-regressing.example", client="Client Trend"),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Trend Regressing",
            url="https://trend-regressing.example",
            client="Client Trend",
            uptime_ok="false",
            ssl_days="5",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2200",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Trend Improving",
            url="https://trend-improving.example",
            client="Client Trend",
            uptime_ok="false",
            ssl_days="5",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2200",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Trend Improving", url="https://trend-improving.example", client="Client Trend"),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Trend New", url="https://trend-new.example", client="Client New"),
        follow_redirects=False,
    )

    response = client.get("/api/site-trends?limit=10")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["snapshot_limit"] == 10
    assert payload["status"] == "red"
    assert payload["site_count"] == 3
    assert payload["regressing_count"] == 1
    assert payload["improving_count"] == 1
    assert payload["new_count"] == 1
    assert [trend["trend_status"] for trend in payload["trends"]] == ["regressing", "new", "improving"]
    regressing = payload["trends"][0]
    assert regressing["name"] == "Trend Regressing"
    assert regressing["score_delta"] < 0
    assert regressing["previous_score"] > regressing["latest_score"]
    assert regressing["recommended_action"] == "Review recent changes and open a remediation task for the regression."
    improving = payload["trends"][2]
    assert improving["name"] == "Trend Improving"
    assert improving["score_delta"] > 0
    assert improving["recommended_action"] == "Continue monitoring the site trend."


def test_api_site_trends_fail_closed_when_latest_snapshot_is_stale(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Trend Site",
            url="https://stale-trend.example",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Trend Site",
            url="https://stale-trend.example",
            uptime_ok="false",
            ssl_days="3",
        ),
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute(
            "update snapshots set captured_at = ? where id = (select max(id) from snapshots)",
            ("2000-01-01 00:00:00",),
        )

    payload = client.get("/api/site-trends").json()

    assert payload["site_count"] == 1
    assert payload["status"] == "yellow"
    assert payload["current_evidence_count"] == 0
    assert payload["unknown_count"] == 1
    assert payload["trend_evidence_percent"] == 0
    assert payload["regressing_count"] == 0
    trend = payload["trends"][0]
    assert trend["trend_status"] == "unknown"
    assert trend["latest_score"] is None
    assert trend["previous_score"] is None
    assert trend["score_delta"] is None
    assert trend["observed_latest_score"] < trend["observed_previous_score"]
    assert trend["observed_score_delta"] < 0
    assert trend["observed_trend_status"] == "regressing"
    assert trend["snapshot_freshness"] == "stale"
    assert trend["snapshot_age_hours"] > 168
    assert trend["recommended_action"] == (
        "Capture a fresh fleet snapshot before relying on site trend status."
    )


def test_api_site_trends_prevents_one_site_from_crowding_out_other_histories(tmp_path):
    client = make_test_client(tmp_path)
    for response_ms in (300, 1800):
        client.post(
            "/snapshot",
            data=valid_snapshot_payload(
                name="Quiet Trend Site",
                url="https://quiet-trend.example",
                response_ms=str(response_ms),
            ),
            follow_redirects=False,
        )
    for response_ms in (200, 300, 400, 500):
        client.post(
            "/snapshot",
            data=valid_snapshot_payload(
                name="Frequent Trend Site",
                url="https://frequent-trend.example",
                response_ms=str(response_ms),
            ),
            follow_redirects=False,
        )

    response = client.get("/api/site-trends?limit=4")

    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshot_limit"] == 4
    assert payload["site_count"] == 2
    trends = {trend["name"]: trend for trend in payload["trends"]}
    assert trends["Quiet Trend Site"]["previous_score"] is not None
    assert trends["Quiet Trend Site"]["trend_status"] == "regressing"
    assert trends["Frequent Trend Site"]["previous_score"] is not None


def test_api_action_matrix_groups_open_actions_by_client_and_site(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Matrix Store",
            url="https://critical-matrix.example",
            client="Client Matrix",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="120",
            response_ms="2600",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Warning Matrix Blog",
            url="https://warning-matrix.example",
            client="Client Matrix",
            wp_updates="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Other Warning",
            url="https://other-warning-matrix.example",
            client="Other Client",
            wp_updates="1",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/action-matrix")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["client_count"] == 2
    assert payload["site_count"] == 3
    assert payload["open_action_count"] >= 4
    assert payload["critical_action_count"] >= 1
    assert payload["warning_action_count"] >= 2
    assert [row["client"] for row in payload["clients"]] == ["Client Matrix", "Other Client"]
    matrix = payload["clients"][0]
    assert matrix["site_count"] == 2
    assert matrix["critical_action_count"] >= 1
    assert matrix["warning_action_count"] >= 1
    assert matrix["lowest_score"] < 70
    assert matrix["latest_snapshot_at"]
    assert [site["site"] for site in matrix["sites"]] == ["Critical Matrix Store", "Warning Matrix Blog"]
    assert matrix["sites"][0]["top_severity"] == "critical"
    assert matrix["sites"][0]["top_recommended_action"] == "Confirm site availability, hosting status, and recent deploys."
    assert matrix["sites"][1]["top_severity"] == "warning"


def test_api_site_priorities_returns_bounded_dispatch_queue(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Priority Critical Store",
            url="https://priority-critical.example",
            client="Client Priority",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="120",
            response_ms="2600",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Priority Warning Blog",
            url="https://priority-warning.example",
            client="Client Priority",
            wp_updates="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Priority Healthy Site", url="https://priority-healthy.example"),
        follow_redirects=False,
    )

    response = client.get("/api/site-priorities?limit=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["limit"] == 1
    assert payload["site_count"] == 3
    assert payload["priority_site_count"] == 2
    assert payload["returned_site_count"] == 1
    top_site = payload["sites"][0]
    assert top_site["name"] == "Priority Critical Store"
    assert top_site["client"] == "Client Priority"
    assert top_site["priority_score"] > 100
    assert top_site["critical_alert_count"] >= 1
    assert top_site["warning_alert_count"] >= 1
    assert top_site["top_severity"] == "critical"
    assert top_site["next_action"] == "Confirm site availability, hosting status, and recent deploys."


def test_api_client_priorities_rolls_up_dispatch_priority_by_account(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Client A Critical",
            url="https://client-a-critical.example",
            client="Client A",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="120",
            response_ms="2600",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Client A Warning",
            url="https://client-a-warning.example",
            client="Client A",
            wp_updates="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Client B Warning",
            url="https://client-b-warning.example",
            client="Client B",
            wp_updates="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Client C Healthy", url="https://client-c-healthy.example", client="Client C"),
        follow_redirects=False,
    )

    response = client.get("/api/client-priorities?limit=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["limit"] == 1
    assert payload["client_count"] == 2
    assert payload["returned_client_count"] == 1
    assert payload["total_priority_score"] > 0
    assert len(payload["clients"]) == 1
    client_a = payload["clients"][0]
    assert client_a["client"] == "Client A"
    assert client_a["priority_site_count"] == 2
    assert client_a["priority_score"] > client_a["top_site_priority_score"] > 100
    assert client_a["critical_alert_count"] >= 1
    assert client_a["warning_alert_count"] >= 1
    assert client_a["lowest_score"] < 70
    assert client_a["top_site"] == "Client A Critical"
    assert client_a["latest_snapshot_at"]


def test_api_operations_kpis_returns_management_rollup(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="KPI Critical Store",
            url="https://kpi-critical.example",
            client="Client KPI Critical",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="KPI Warning Blog",
            url="https://kpi-warning.example",
            client="Client KPI Warning",
            wp_updates="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="KPI Healthy Site",
            url="https://kpi-healthy.example",
            client="Client KPI Healthy",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/operations-kpis")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["status"] == "red"
    assert payload["site_count"] == 3
    assert payload["red_site_count"] == 1
    assert payload["green_site_count"] == 2
    assert payload["open_action_count"] >= 6
    assert payload["immediate_action_count"] >= 1
    assert payload["scheduled_action_count"] >= 1
    assert payload["approval_needed_count"] == 2
    assert payload["priority_site_count"] == 2
    assert payload["top_priority_site"] == "KPI Critical Store"
    assert payload["recommended_focus"] == "Confirm site availability, hosting status, and recent deploys."


def test_api_operations_kpis_warns_about_monitoring_gaps(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Stale KPI Site", url="https://stale-kpi.example"),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={"name": "Missing KPI Site", "url": "https://missing-kpi.example"},
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    response = client.get("/api/operations-kpis")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "yellow"
    assert payload["site_count"] == 2
    assert payload["monitored_site_count"] == 1
    assert payload["missing_snapshot_count"] == 1
    assert payload["stale_snapshot_count"] == 1
    assert payload["monitoring_coverage_percent"] == 50
    assert payload["snapshot_freshness_percent"] == 0
    assert payload["recommended_focus"] == "Capture initial fleet snapshots for unmonitored sites."


def test_api_client_update_briefs_returns_client_facing_status_notes(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Critical Client Update",
            url="https://critical-update.example",
            client="Client Update Critical",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Warning Client Update",
            url="https://warning-update.example",
            client="Client Update Warning",
            wp_updates="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Healthy Client Update",
            url="https://healthy-update.example",
            client="Client Update Healthy",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/client-update-briefs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["client_count"] == 3
    assert payload["red_count"] == 1
    assert payload["yellow_count"] == 1
    assert payload["green_count"] == 1
    assert [row["client"] for row in payload["clients"]] == [
        "Client Update Critical",
        "Client Update Warning",
        "Client Update Healthy",
    ]
    critical = payload["clients"][0]
    assert critical["status"] == "red"
    assert critical["headline"] == "Client Update Critical: RED status across 1 tracked site."
    assert critical["open_action_count"] >= 5
    assert critical["immediate_action_count"] >= 1
    assert critical["scheduled_action_count"] >= 1
    assert critical["healthy_site_count"] == 0
    assert critical["top_site"] == "Critical Client Update"
    assert critical["next_action"] == "Confirm site availability, hosting status, and recent deploys."
    assert "0 sites are healthy" in critical["client_message"]
    warning = payload["clients"][1]
    assert warning["status"] == "yellow"
    assert warning["open_action_count"] == 1
    assert warning["client_message"] == "1 site is healthy; 1 open action remains in the work queue."
    healthy = payload["clients"][2]
    assert healthy["status"] == "green"
    assert healthy["healthy_site_count"] == 1
    assert healthy["open_action_count"] == 0
    assert healthy["next_action"] == "Continue normal monitoring cadence."


def test_api_client_update_briefs_warn_about_monitoring_gaps(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Update Site",
            url="https://stale-update-brief.example",
            client="Client Update Gap",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Update Site",
            "url": "https://missing-update-brief.example",
            "client": "Client Update Gap",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    payload = client.get("/api/client-update-briefs").json()

    assert payload["client_count"] == 1
    assert payload["yellow_count"] == 1
    assert payload["missing_snapshot_count"] == 1
    assert payload["stale_snapshot_count"] == 1
    brief = payload["clients"][0]
    assert brief["client"] == "Client Update Gap"
    assert brief["status"] == "yellow"
    assert brief["site_count"] == 2
    assert brief["monitored_site_count"] == 1
    assert brief["current_snapshot_count"] == 0
    assert brief["missing_snapshot_count"] == 1
    assert brief["stale_snapshot_count"] == 1
    assert brief["monitoring_coverage_percent"] == 50
    assert brief["snapshot_freshness_percent"] == 0
    assert brief["healthy_site_count"] == 0
    assert brief["open_action_count"] == 0
    assert brief["top_site"] == "Missing Update Site"
    assert brief["next_action"] == "Capture initial fleet snapshots for unmonitored sites."
    assert brief["client_message"] == (
        "0 of 2 tracked sites have current snapshots; 2 monitoring gaps require follow-up."
    )


def test_api_client_service_reviews_prioritizes_account_checkins(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Urgent Review Store",
            url="https://urgent-review.example",
            client="Client Urgent",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Scheduled Review Blog",
            url="https://scheduled-review.example",
            client="Client Scheduled",
            wp_updates="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Routine Review Site",
            url="https://routine-review.example",
            client="Client Routine",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/client-service-reviews")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["client_count"] == 3
    assert payload["urgent_review_count"] == 1
    assert payload["scheduled_review_count"] == 1
    assert payload["routine_review_count"] == 1
    assert [row["client"] for row in payload["clients"]] == ["Client Urgent", "Client Scheduled", "Client Routine"]
    urgent = payload["clients"][0]
    assert urgent["status"] == "red"
    assert urgent["review_priority"] == "urgent"
    assert urgent["top_site"] == "Urgent Review Store"
    assert urgent["talking_point"] == "Review urgent incidents, backup readiness, and maintenance approvals."
    assert urgent["next_action"] == "Confirm site availability, hosting status, and recent deploys."
    scheduled = payload["clients"][1]
    assert scheduled["status"] == "yellow"
    assert scheduled["review_priority"] == "scheduled"
    assert scheduled["talking_point"] == "Review scheduled maintenance timing and open work queue ownership."
    routine = payload["clients"][2]
    assert routine["status"] == "green"
    assert routine["review_priority"] == "routine"
    assert routine["open_action_count"] == 0
    assert routine["talking_point"] == "Review monitoring coverage, recent wins, and upcoming maintenance cadence."


def test_api_client_service_reviews_schedule_monitoring_gap_follow_up(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Review Site",
            url="https://stale-service-review.example",
            client="Client Review Gap",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Review Site",
            "url": "https://missing-service-review.example",
            "client": "Client Review Gap",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    payload = client.get("/api/client-service-reviews").json()

    assert payload["client_count"] == 1
    assert payload["scheduled_review_count"] == 1
    assert payload["monitoring_gap_client_count"] == 1
    review = payload["clients"][0]
    assert review["client"] == "Client Review Gap"
    assert review["status"] == "yellow"
    assert review["review_priority"] == "scheduled"
    assert review["monitoring_gap_count"] == 2
    assert review["missing_snapshot_count"] == 1
    assert review["stale_snapshot_count"] == 1
    assert review["current_snapshot_count"] == 0
    assert review["top_site"] == "Missing Review Site"
    assert review["talking_point"] == "Restore monitoring coverage before the next client review."
    assert review["next_action"] == "Capture initial fleet snapshots for unmonitored sites."


def test_api_client_follow_ups_adds_due_dates_and_channels(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Urgent Follow Up Store",
            url="https://urgent-follow-up.example",
            client="Client Follow Urgent",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Scheduled Follow Up Blog",
            url="https://scheduled-follow-up.example",
            client="Client Follow Scheduled",
            wp_updates="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Routine Follow Up Site",
            url="https://routine-follow-up.example",
            client="Client Follow Routine",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/client-follow-ups")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["follow_up_count"] == 3
    assert payload["urgent_count"] == 1
    assert payload["scheduled_count"] == 1
    assert payload["routine_count"] == 1
    assert [row["client"] for row in payload["follow_ups"]] == [
        "Client Follow Urgent",
        "Client Follow Scheduled",
        "Client Follow Routine",
    ]
    urgent = payload["follow_ups"][0]
    assert urgent["priority"] == "urgent"
    assert urgent["due"] == "today"
    assert urgent["channel"] == "phone"
    assert urgent["top_site"] == "Urgent Follow Up Store"
    assert urgent["next_action"] == "Confirm site availability, hosting status, and recent deploys."
    scheduled = payload["follow_ups"][1]
    assert scheduled["priority"] == "scheduled"
    assert scheduled["due"] == "this week"
    assert scheduled["channel"] == "ticket"
    routine = payload["follow_ups"][2]
    assert routine["priority"] == "routine"
    assert routine["due"] == "next account review"
    assert routine["channel"] == "email"
    assert routine["open_action_count"] == 0


def test_api_maintenance_approval_packets_summarizes_client_approvals(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Urgent Approval Store",
            url="https://urgent-approval.example",
            client="Client Approval Urgent",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Scheduled Approval Blog",
            url="https://scheduled-approval.example",
            client="Client Approval Scheduled",
            wp_updates="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Routine Approval Site",
            url="https://routine-approval.example",
            client="Client Approval Routine",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/maintenance-approval-packets")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["packet_count"] == 3
    assert payload["needed_count"] == 2
    assert payload["urgent_count"] == 1
    assert payload["scheduled_count"] == 1
    assert [packet["client"] for packet in payload["packets"]] == [
        "Client Approval Urgent",
        "Client Approval Scheduled",
        "Client Approval Routine",
    ]
    urgent = payload["packets"][0]
    assert urgent["approval_priority"] == "urgent"
    assert urgent["approval_window"] == "same-day approval"
    assert urgent["packet_needed"] is True
    assert urgent["top_site"] == "Urgent Approval Store"
    assert urgent["approval_summary"].startswith("Request urgent maintenance approval")
    scheduled = payload["packets"][1]
    assert scheduled["approval_window"] == "next maintenance window"
    assert scheduled["packet_needed"] is True
    routine = payload["packets"][2]
    assert routine["approval_priority"] == "routine"
    assert routine["approval_window"] == "next account review"
    assert routine["packet_needed"] is False
    assert routine["approval_summary"] == "No maintenance approval packet is needed for Client Approval Routine right now."


def test_monitoring_gaps_do_not_request_client_maintenance_approval(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Approval Site",
            url="https://stale-approval.example",
            client="Client Monitoring Gap",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Approval Site",
            "url": "https://missing-approval.example",
            "client": "Client Monitoring Gap",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    approval_payload = client.get("/api/maintenance-approval-packets").json()
    draft_payload = client.get("/api/maintenance-ticket-drafts").json()

    assert approval_payload["scheduled_count"] == 1
    assert approval_payload["needed_count"] == 0
    packet = approval_payload["packets"][0]
    assert packet["client"] == "Client Monitoring Gap"
    assert packet["approval_priority"] == "scheduled"
    assert packet["open_action_count"] == 0
    assert packet["packet_needed"] is False
    assert packet["approval_summary"] == (
        "No maintenance approval packet is needed for Client Monitoring Gap right now."
    )
    assert draft_payload["draft_count"] == 0
    assert draft_payload["drafts"] == []


def test_api_maintenance_ticket_drafts_returns_ticket_ready_approval_requests(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Ticket Urgent Store",
            url="https://ticket-urgent.example",
            client="Client Ticket Urgent",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="96",
            response_ms="2200",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Ticket Routine Site",
            url="https://ticket-routine.example",
            client="Client Ticket Routine",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/maintenance-ticket-drafts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["draft_count"] == 1
    assert payload["urgent_count"] == 1
    assert payload["scheduled_count"] == 0
    draft = payload["drafts"][0]
    assert draft["client"] == "Client Ticket Urgent"
    assert draft["priority"] == "urgent"
    assert draft["approval_window"] == "same-day approval"
    assert draft["subject"] == "Client Ticket Urgent: Urgent maintenance approval request"
    assert "Request urgent maintenance approval" in draft["body"]
    assert "Top site: Ticket Urgent Store" in draft["body"]
    assert "Suggested timing: same-day approval" in draft["body"]
    assert draft["top_site"] == "Ticket Urgent Store"
    assert draft["open_action_count"] >= 1


def test_api_dispatch_summary_returns_queue_level_operator_routing(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Dispatch Critical Store",
            url="https://dispatch-critical.example",
            client="Client Dispatch Critical",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Dispatch Warning Blog",
            url="https://dispatch-warning.example",
            client="Client Dispatch Warning",
            wp_updates="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Dispatch Healthy Site", url="https://dispatch-healthy.example"),
        follow_redirects=False,
    )

    response = client.get("/api/dispatch-summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["status"] == "red"
    assert payload["open_action_count"] >= 6
    assert payload["immediate_action_count"] >= 1
    assert payload["scheduled_action_count"] >= 1
    assert payload["priority_site_count"] == 2
    assert payload["top_client"] == "Client Dispatch Critical"
    assert payload["top_client_open_action_count"] >= 5
    assert payload["top_site"] == "Dispatch Critical Store"
    assert payload["top_action"] == "Confirm site availability, hosting status, and recent deploys."
    assert payload["next_queue"] == "immediate"
    assert [site["name"] for site in payload["priority_sites"]] == ["Dispatch Critical Store", "Dispatch Warning Blog"]


def test_dispatch_and_daily_brief_route_monitoring_gaps_to_operators(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Stale Dispatch Site",
            url="https://stale-dispatch.example",
            client="Client Dispatch Gap",
        ),
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Missing Dispatch Site",
            "url": "https://missing-dispatch.example",
            "client": "Client Dispatch Gap",
        },
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    dispatch = client.get("/api/dispatch-summary").json()

    assert dispatch["status"] == "yellow"
    assert dispatch["open_action_count"] == 0
    assert dispatch["missing_snapshot_count"] == 1
    assert dispatch["stale_snapshot_count"] == 1
    assert dispatch["monitoring_gap_count"] == 2
    assert dispatch["top_client"] == "Client Dispatch Gap"
    assert dispatch["top_site"] == "Missing Dispatch Site"
    assert dispatch["top_action"] == "Capture a fresh fleet snapshot and verify site health."
    assert dispatch["next_queue"] == "monitoring"

    brief = client.get("/api/daily-ops-brief").json()

    assert brief["status"] == "yellow"
    assert brief["headline"] == (
        "Yellow shift brief: 2 monitoring gaps need follow-up. Start with Missing Dispatch Site."
    )
    assert brief["missing_snapshot_count"] == 1
    assert brief["stale_snapshot_count"] == 1
    assert brief["monitoring_gap_count"] == 2
    assert brief["next_queue"] == "monitoring"
    assert brief["top_site"] == "Missing Dispatch Site"
    assert brief["recommended_focus"] == "Capture a fresh fleet snapshot and verify site health."


def test_api_daily_ops_brief_returns_shift_ready_summary(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Brief Critical Store",
            url="https://brief-critical.example",
            client="Client Brief Critical",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Brief Scheduled Blog",
            url="https://brief-scheduled.example",
            client="Client Brief Scheduled",
            wp_updates="1",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/daily-ops-brief")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["status"] == "red"
    assert payload["headline"].startswith("Red shift brief:")
    assert payload["site_count"] == 2
    assert payload["critical_alerts"] >= 1
    assert payload["next_queue"] == "immediate"
    assert payload["top_client"] == "Client Brief Critical"
    assert payload["top_site"] == "Brief Critical Store"
    assert payload["recommended_focus"] == "Confirm site availability, hosting status, and recent deploys."
    assert payload["priority_sites"][0]["name"] == "Brief Critical Store"


def test_api_account_agenda_returns_bounded_weekly_service_plan(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Agenda Critical Store",
            url="https://agenda-critical.example",
            client="Client Agenda Critical",
            uptime_ok="false",
            ssl_days="3",
            wp_updates="6",
            backup_age_hours="100",
            response_ms="2400",
            security_header_count="0",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Agenda Scheduled Blog",
            url="https://agenda-scheduled.example",
            client="Client Agenda Scheduled",
            wp_updates="1",
        ),
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Agenda Routine Site",
            url="https://agenda-routine.example",
            client="Client Agenda Routine",
        ),
        follow_redirects=False,
    )

    response = client.get("/api/account-agenda?limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["limit"] == 2
    assert payload["account_count"] == 3
    assert payload["returned_account_count"] == 2
    assert payload["urgent_count"] == 1
    assert payload["scheduled_count"] == 1
    assert payload["routine_count"] == 1
    assert [item["client"] for item in payload["agenda"]] == ["Client Agenda Critical", "Client Agenda Scheduled"]
    urgent = payload["agenda"][0]
    assert urgent["priority"] == "urgent"
    assert urgent["focus"] == "incident response"
    assert urgent["top_site"] == "Agenda Critical Store"
    assert urgent["next_action"] == "Confirm site availability, hosting status, and recent deploys."
    scheduled = payload["agenda"][1]
    assert scheduled["priority"] == "scheduled"
    assert scheduled["focus"] == "maintenance planning"
    assert scheduled["talking_point"] == "Review scheduled maintenance timing and open work queue ownership."


def test_api_account_agenda_prioritizes_monitoring_restoration(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/sites",
        data={
            "name": "Single Gap Site",
            "url": "https://single-agenda-gap.example",
            "client": "A Single Gap Client",
        },
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "First Multi Gap Site",
            "url": "https://first-multi-agenda-gap.example",
            "client": "Z Multi Gap Client",
        },
        follow_redirects=False,
    )
    client.post(
        "/sites",
        data={
            "name": "Second Multi Gap Site",
            "url": "https://second-multi-agenda-gap.example",
            "client": "Z Multi Gap Client",
        },
        follow_redirects=False,
    )

    response = client.get("/api/account-agenda?limit=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["account_count"] == 2
    assert payload["returned_account_count"] == 1
    assert payload["monitoring_gap_account_count"] == 2
    assert payload["monitoring_gap_count"] == 3
    item = payload["agenda"][0]
    assert item["client"] == "Z Multi Gap Client"
    assert item["priority"] == "scheduled"
    assert item["focus"] == "monitoring restoration"
    assert item["current_snapshot_count"] == 0
    assert item["missing_snapshot_count"] == 2
    assert item["stale_snapshot_count"] == 0
    assert item["monitoring_gap_count"] == 2
    assert item["top_site"] == "First Multi Gap Site"
    assert item["next_action"] == "Capture initial fleet snapshots for unmonitored sites."


def test_dashboard_exposes_live_care_check_action(tmp_path):
    client = make_test_client(tmp_path)

    page = client.get("/")

    assert page.status_code == 200
    assert 'formaction="/care/fetch-check"' in page.text
    assert "Run live check" in page.text


def test_fetch_check_populates_fleet_dashboard_snapshot(tmp_path, monkeypatch):
    client = make_test_client(tmp_path)

    def fake_fetch(name, url):
        from wp_fleet_ops.checks import evaluate_site

        return evaluate_site(
            name,
            url,
            200,
            321,
            45,
            "6.6.2",
            2,
            18,
            {"Strict-Transport-Security": "max-age=31536000", "X-Frame-Options": "SAMEORIGIN"},
        )

    import wp_fleet_ops.main as main

    monkeypatch.setattr(main, "fetch_basic_site_check", fake_fetch)
    response = client.post(
        "/care/fetch-check",
        data={"name": "Fetched Site", "url": "fetched.example", "client": "Client F"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    page = client.get("/").text
    assert "Fetched Site" in page
    assert "321ms" in page
    report = client.get("/report").text
    assert "Fetched Site" in report
    assert "2 WordPress updates pending" in report


def test_report_preserves_fetched_security_header_coverage(tmp_path, monkeypatch):
    client = make_test_client(tmp_path)

    def fake_fetch(name, url):
        from wp_fleet_ops.checks import evaluate_site

        return evaluate_site(
            name,
            url,
            200,
            180,
            60,
            "6.6.2",
            0,
            12,
            {
                "Strict-Transport-Security": "max-age=31536000",
                "Content-Security-Policy": "frame-ancestors 'self'",
            },
        )

    import wp_fleet_ops.main as main

    monkeypatch.setattr(main, "fetch_basic_site_check", fake_fetch)
    response = client.post(
        "/care/fetch-check",
        data={"name": "Secure Site", "url": "https://secure.example", "client": "Secure Client"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    report = client.get("/report").text
    assert "Secure Site" in report
    assert "Score: 100/100" in report
    assert "Add or verify HSTS security header." not in report
    assert "Add clickjacking protection header." not in report


def test_manual_check_and_snapshot_share_canonical_url_handling(tmp_path):
    client = make_test_client(tmp_path)

    manual = client.post(
        "/care/manual-check",
        data={"name": "Canonical Site", "url": "Example.COM/", "client": "Canonical Client"},
        follow_redirects=False,
    )
    snapshot = client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Canonical Site", url="HTTPS://example.com/", client="Canonical Client"),
        follow_redirects=False,
    )

    assert manual.status_code == 303
    assert snapshot.status_code == 303
    directory = client.get("/api/site-directory").json()
    assert directory["site_count"] == 1
    assert directory["sites"][0]["url"] == "https://example.com"


def test_snapshot_report_preserves_security_header_coverage(tmp_path):
    client = make_test_client(tmp_path)

    response = client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="Secure Snapshot",
            url="https://secure-snapshot.example",
            security_header_count="3",
        ),
        follow_redirects=False,
    )

    assert response.status_code == 303
    report = client.get("/report").text
    assert "Secure Snapshot" in report
    assert "Score: 100/100" in report
    assert "Add or verify HSTS security header." not in report
    assert "Add clickjacking protection header." not in report


def test_snapshot_rejects_invalid_metrics_and_urls(tmp_path):
    client = make_test_client(tmp_path)
    assert client.post("/snapshot", data=valid_snapshot_payload(ssl_days="-1"), follow_redirects=False).status_code == 422
    assert client.post("/snapshot", data=valid_snapshot_payload(url="javascript:alert(1)"), follow_redirects=False).status_code == 422


def test_snapshot_rejects_security_header_counts_above_monitored_set(tmp_path):
    client = make_test_client(tmp_path)

    response = client.post(
        "/snapshot",
        data=valid_snapshot_payload(security_header_count="4"),
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert client.get("/api/site-directory").json()["site_count"] == 0


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://admin:secret@example.com"])
def test_fetch_check_rejects_unsafe_urls_before_persisting(tmp_path, url):
    client = make_test_client(tmp_path)

    response = client.post(
        "/care/fetch-check",
        data={"name": "Unsafe Site", "url": url},
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Site URL must be a valid HTTP or HTTPS URL."}
    assert client.get("/api/site-directory").json()["site_count"] == 0


def test_manual_care_check_rejects_invalid_operational_metrics(tmp_path):
    client = make_test_client(tmp_path)
    payload = {"name": "Bad Metrics", "url": "https://bad.example", "latency_ms": "-25"}
    assert client.post("/care/manual-check", data=payload, follow_redirects=False).status_code == 422

    payload = {"name": "Bad Status", "url": "https://bad.example", "http_status": "700"}
    assert client.post("/care/manual-check", data=payload, follow_redirects=False).status_code == 422


def test_snapshot_normalizes_site_name_in_alert_payloads_and_reports(tmp_path):
    client = make_test_client(tmp_path)

    response = client.post(
        "/snapshot",
        data=valid_snapshot_payload(
            name="  Padded Site  ",
            url="https://padded.example",
            uptime_ok="false",
        ),
        follow_redirects=False,
    )

    assert response.status_code == 303
    site = client.get("/api/sites").json()["sites"][0]
    assert site["name"] == "Padded Site"
    assert site["alerts"][0]["site"] == "Padded Site"
    assert site["alerts"][0]["message"] == "Padded Site appears down or unreachable."
    report = client.get("/report").text
    assert "Padded Site needs attention" in report
    assert "  Padded Site  " not in report


def test_api_site_snapshot_history_returns_ordered_snapshots(tmp_path):
    client = make_test_client(tmp_path)
    for i in range(3):
        client.post(
            "/snapshot",
            data=valid_snapshot_payload(
                name="History Site",
                url="https://history.example",
                response_ms=str(200 + i * 100),
            ),
            follow_redirects=False,
        )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Other Site", url="https://other.example"),
        follow_redirects=False,
    )

    response = client.get("/api/site-snapshot-history?url=https://history.example")

    assert response.status_code == 200
    payload = response.json()
    assert payload["url"] == "https://history.example"
    assert payload["snapshot_count"] == 3
    # All snapshots belong to the requested site
    assert all(s["url"] == "https://history.example" for s in payload["snapshots"])
    # Newest first (last inserted has highest response_ms=400)
    assert payload["snapshots"][0]["response_ms"] == 400
    assert payload["snapshots"][1]["response_ms"] == 300


def test_api_site_snapshot_history_respects_limit(tmp_path):
    client = make_test_client(tmp_path)
    for i in range(5):
        client.post(
            "/snapshot",
            data=valid_snapshot_payload(
                name="Paged Site",
                url="https://paged.example",
                response_ms=str(200 + i * 100),
            ),
            follow_redirects=False,
        )

    response = client.get("/api/site-snapshot-history?url=https://paged.example&limit=2&offset=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["limit"] == 2
    assert payload["offset"] == 2
    assert payload["snapshot_count"] == 2
    assert payload["total_snapshot_count"] == 5
    assert payload["has_more"] is True
    assert payload["page_number"] == 2
    assert payload["page_count"] == 3
    assert payload["previous_offset"] == 0
    assert payload["next_offset"] == 4
    assert payload["snapshots"][0]["snapshot_id"] > payload["snapshots"][1]["snapshot_id"]
    assert [snapshot["response_ms"] for snapshot in payload["snapshots"]] == [400, 300]


def test_api_site_snapshot_history_returns_empty_for_unknown_site(tmp_path):
    client = make_test_client(tmp_path)

    response = client.get("/api/site-snapshot-history?url=https://unknown.example")

    assert response.status_code == 200
    payload = response.json()
    assert payload["url"] == "https://unknown.example"
    assert payload["snapshot_count"] == 0
    assert payload["total_snapshot_count"] == 0
    assert payload["has_more"] is False
    assert payload["first_offset"] is None
    assert payload["last_offset"] is None
    assert payload["previous_offset"] is None
    assert payload["next_offset"] is None
    assert payload["snapshots"] == []
    assert payload["generated_at"].endswith("+00:00")


def test_api_care_check_history_returns_paginated_checks_newest_first(tmp_path):
    client = make_test_client(tmp_path)
    for i in range(4):
        client.post(
            "/care/manual-check",
            data={
                "name": f"Care History {i}",
                "url": f"https://care-history-{i}.example",
                "client": "Client Care",
                "latency_ms": str(200 + i * 100),
                "wordpress_version": f"6.{i}",
                "update_count": str(i),
            },
            follow_redirects=False,
        )

    response = client.get("/api/care-check-history?limit=2&offset=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"].endswith("+00:00")
    assert payload["url"] is None
    assert payload["limit"] == 2
    assert payload["offset"] == 1
    assert payload["care_check_count"] == 2
    assert payload["total_care_check_count"] == 4
    assert payload["has_more"] is True
    assert payload["previous_offset"] == 0
    assert payload["next_offset"] == 3
    assert payload["care_checks"][0]["care_check_id"] > payload["care_checks"][1]["care_check_id"]
    assert [check["wordpress_version"] for check in payload["care_checks"]] == ["6.2", "6.1"]
    assert payload["care_checks"][0]["client"] == "Client Care"
    assert payload["care_checks"][0]["actions"]


def test_api_care_check_history_filters_by_normalized_site_url(tmp_path):
    client = make_test_client(tmp_path)
    for latency_ms in (200, 500, 800):
        client.post(
            "/care/manual-check",
            data={
                "name": "Care History Site",
                "url": "https://care-history.example",
                "latency_ms": str(latency_ms),
            },
            follow_redirects=False,
        )
    client.post(
        "/care/manual-check",
        data={"name": "Other Care Site", "url": "https://other-care.example"},
        follow_redirects=False,
    )

    response = client.get(
        "/api/care-check-history",
        params={"url": "HTTPS://CARE-HISTORY.EXAMPLE:443/#checks", "limit": 2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["url"] == "https://care-history.example"
    assert payload["care_check_count"] == 2
    assert payload["total_care_check_count"] == 3
    assert payload["has_more"] is True
    assert [check["latency_ms"] for check in payload["care_checks"]] == [800, 500]
    assert all(check["url"] == payload["url"] for check in payload["care_checks"])


def test_delete_site_removes_site_and_cascades_history(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/care/manual-check",
        data={"name": "Doomed Site", "url": "https://doomed.example", "client": "Acme"},
        follow_redirects=False,
    )
    client.post(
        "/snapshot",
        data=valid_snapshot_payload(name="Doomed Site", url="https://doomed.example", client="Acme"),
        follow_redirects=False,
    )
    # manual-check saves both a care_check and a snapshot; the extra /snapshot
    # POST also saves a care_check and a snapshot → 1 site, 2 care_checks, 2 fleet_snapshots.
    ready_before = client.get("/ready").json()
    assert ready_before["sites"] == 1
    assert ready_before["care_checks"] == 2
    assert ready_before["fleet_snapshots"] == 2

    response = client.delete("/sites", params={"url": "https://doomed.example"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] is True
    assert payload["url"] == "https://doomed.example"
    ready_after = client.get("/ready").json()
    assert ready_after["sites"] == 0
    assert ready_after["care_checks"] == 0
    assert ready_after["fleet_snapshots"] == 0


def test_delete_site_normalizes_url_before_lookup(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/care/manual-check",
        data={"name": "Normalize Me", "url": "https://normalize.example"},
        follow_redirects=False,
    )

    # Submit URL with redundant port, uppercase scheme, trailing fragment
    response = client.delete(
        "/sites",
        params={"url": "HTTPS://NORMALIZE.EXAMPLE:443/#gone"},
    )

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert client.get("/ready").json()["sites"] == 0


def test_delete_site_returns_404_for_unknown_url(tmp_path):
    client = make_test_client(tmp_path)

    response = client.delete("/sites", params={"url": "https://no-such-site.example"})

    assert response.status_code == 404
    payload = response.json()
    assert payload["deleted"] is False


def test_delete_site_does_not_affect_other_sites(tmp_path):
    client = make_test_client(tmp_path)
    client.post(
        "/care/manual-check",
        data={"name": "Keep Me", "url": "https://keep.example"},
        follow_redirects=False,
    )
    client.post(
        "/care/manual-check",
        data={"name": "Remove Me", "url": "https://remove.example"},
        follow_redirects=False,
    )

    response = client.delete("/sites", params={"url": "https://remove.example"})

    assert response.status_code == 200
    ready = client.get("/ready").json()
    assert ready["sites"] == 1
    assert ready["care_checks"] == 1
    # manual-check saves one fleet snapshot per call; removing one site leaves one
    assert ready["fleet_snapshots"] == 1
    directory = client.get("/api/site-directory").json()
    assert len(directory["sites"]) == 1
    assert directory["sites"][0]["url"] == "https://keep.example"


def test_delete_site_returns_422_for_invalid_url(tmp_path):
    client = make_test_client(tmp_path)

    # A URL with embedded spaces is rejected by normalize_site_url before the
    # store is consulted; the ValueError exception handler returns 422.
    response = client.delete("/sites", params={"url": "not a valid url"})

    assert response.status_code == 422


def test_api_care_check_history_filters_and_paginates_by_client(tmp_path):
    client = make_test_client(tmp_path)
    for i in range(3):
        client.post(
            "/care/manual-check",
            data={
                "name": f"Alpha Care History {i}",
                "url": f"https://alpha-care-history-{i}.example",
                "client": "Client Alpha",
                "latency_ms": str(300 + i * 100),
            },
            follow_redirects=False,
        )
    client.post(
        "/care/manual-check",
        data={
            "name": "Beta Care History",
            "url": "https://beta-care-history.example",
            "client": "Client Beta",
            "latency_ms": "900",
        },
        follow_redirects=False,
    )

    response = client.get(
        "/api/care-check-history",
        params={"client": "  Client Alpha  ", "limit": 2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["url"] is None
    assert payload["client"] == "Client Alpha"
    assert payload["care_check_count"] == 2
    assert payload["total_care_check_count"] == 3
    assert payload["has_more"] is True
    assert [check["latency_ms"] for check in payload["care_checks"]] == [500, 400]
    assert all(check["client"] == payload["client"] for check in payload["care_checks"])
