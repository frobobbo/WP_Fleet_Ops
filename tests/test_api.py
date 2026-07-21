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


def test_responses_include_browser_security_headers(tmp_path):
    client = make_test_client(tmp_path)

    for path in ("/", "/api/summary"):
        response = client.get(path)

        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
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
        data=valid_snapshot_payload(name="Stale Site", url="https://stale-site.example"),
        follow_redirects=False,
    )
    with sqlite3.connect(tmp_path / "test.sqlite3") as con:
        con.execute("update snapshots set captured_at = ?", ("2000-01-01 00:00:00",))

    site = client.get("/api/sites").json()["sites"][0]

    assert site["name"] == "Stale Site"
    assert site["status"] == "green"
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
    assert payload["site_count"] == 4
    assert payload["renewal_count"] == 3
    assert payload["overdue_count"] == 1
    assert payload["immediate_count"] == 1
    assert payload["scheduled_count"] == 1
    assert [site["name"] for site in payload["sites"]] == ["Expired Cert", "Immediate Cert", "Scheduled Cert"]
    assert [site["renewal_window"] for site in payload["sites"]] == ["overdue", "immediate", "scheduled"]
    assert payload["sites"][0]["recommended_action"] == "Replace the expired certificate and verify HTTPS immediately."
    assert payload["sites"][1]["recommended_action"] == "Renew the certificate this week and confirm post-renewal expiry."
    assert payload["sites"][2]["recommended_action"] == "Schedule certificate renewal before the 7-day critical window."


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
    assert entries["updates"]["sites"][0]["recommended_action"].startswith("Apply WordPress")
    assert entries["security"]["sites"][0]["score"] < entries["security"]["sites"][1]["score"]


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
    assert payload["objective_count"] == 5
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
    latest = payload["snapshots"][0]
    assert latest["name"] == "History Site"
    assert latest["client"] == "Client History"
    assert latest["status"] == "red"
    assert latest["uptime_ok"] is False
    assert latest["alert_count"] >= 5
    assert latest["alerts"][0]["severity"] == "critical"


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
