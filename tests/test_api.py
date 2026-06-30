import os
import warnings


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


def test_snapshot_rejects_invalid_metrics_and_urls(tmp_path):
    client = make_test_client(tmp_path)
    assert client.post("/snapshot", data=valid_snapshot_payload(ssl_days="-1"), follow_redirects=False).status_code == 422
    assert client.post("/snapshot", data=valid_snapshot_payload(url="javascript:alert(1)"), follow_redirects=False).status_code == 422


def test_manual_care_check_rejects_invalid_operational_metrics(tmp_path):
    client = make_test_client(tmp_path)
    payload = {"name": "Bad Metrics", "url": "https://bad.example", "latency_ms": "-25"}
    assert client.post("/care/manual-check", data=payload, follow_redirects=False).status_code == 422

    payload = {"name": "Bad Status", "url": "https://bad.example", "http_status": "700"}
    assert client.post("/care/manual-check", data=payload, follow_redirects=False).status_code == 422
