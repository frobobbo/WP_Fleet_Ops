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
