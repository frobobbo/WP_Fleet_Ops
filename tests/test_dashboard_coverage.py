"""Keep dashboard monitoring completeness visible, separate from site health."""
import importlib
import warnings
from contextlib import closing
from html.parser import HTMLParser

import pytest


@pytest.fixture
def coverage_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "coverage.sqlite3"))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        for name in ("first", "second"):
            response = client.post("/snapshot", data={
                "name": name, "url": f"https://{name}.example", "client": "Care Client",
                "uptime_ok": "true", "ssl_days": 90, "wp_updates": 0,
                "backup_age_hours": 12, "response_ms": 100, "security_header_count": 3,
            }, follow_redirects=False)
            assert response.status_code == 303
        yield client, main


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def evidence_section(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="monitoring-evidence"' in response.text
    section = response.text.split('id="monitoring-evidence"', 1)[1].split("</section>", 1)[0]
    parser = TextParser()
    parser.feed(section[section.index(">") + 1:])
    return section, " ".join(" ".join(parser.parts).split())


def test_dashboard_shows_full_paired_coverage_without_claiming_site_health(coverage_client):
    client, main = coverage_client
    # Complete monitoring and critical health are distinct decisions.
    with closing(main.store._connect()) as connection, connection:
        connection.execute("update care_checks set score = 40, status = 'red'")
    section, text = evidence_section(client)
    assert 'aria-label="Monitoring evidence"' in section
    assert '<strong class="green">100%</strong>' in section
    assert "2 of 2 sites have current paired evidence." in text
    assert "Current fleet snapshots: 2 of 2" in text
    assert "Current care checks: 2 of 2" in text
    assert "Monitoring is complete; site health is reported separately." in text
    assert "sites need attention" in client.get("/").text


@pytest.mark.parametrize("table, timestamp", [
    ("snapshots", "captured_at"), ("care_checks", "checked_at"),
])
@pytest.mark.parametrize("value", [
    "2000-01-01 00:00:00", "invalid timestamp", "9999-01-01 00:00:00", None,
])
def test_dashboard_counts_stale_invalid_future_or_missing_sources_as_gaps(
    coverage_client, table, timestamp, value,
):
    client, main = coverage_client
    with closing(main.store._connect()) as connection, connection:
        if value is None:
            connection.execute(f"delete from {table} where site_id = 1")
        else:
            connection.execute(f"update {table} set {timestamp} = ? where site_id = 1", (value,))
    section, text = evidence_section(client)
    assert '<strong class="yellow">50%</strong>' in section
    assert "1 of 2 sites have current paired evidence." in text
    assert "1 site needs monitoring evidence before its health can be verified." in text
    snapshot_count, care_count = (1, 2) if table == "snapshots" else (2, 1)
    assert f"Current fleet snapshots: {snapshot_count} of 2" in text
    assert f"Current care checks: {care_count} of 2" in text
    assert "Monitoring is complete" not in text


def test_dashboard_includes_registry_only_sites_in_coverage(coverage_client):
    client, main = coverage_client
    for name in ("third", "fourth"):
        main.store.upsert_site(name, f"https://{name}.example", "New Client")
    section, text = evidence_section(client)
    assert '<strong class="yellow">50%</strong>' in section
    assert "2 of 4 sites have current paired evidence." in text
    assert "2 sites need monitoring evidence before their health can be verified." in text
    assert "Current fleet snapshots: 2 of 4" in text
    assert "Current care checks: 2 of 4" in text
    assert 'href="#snapshot"' in section
    assert 'href="#care-form"' in section


def test_empty_dashboard_reports_zero_coverage_and_onboarding_step(coverage_client):
    client, main = coverage_client
    for site in main.store.list_sites():
        main.store.delete_site(site["url"])
    section, text = evidence_section(client)
    assert '<strong class="yellow">0%</strong>' in section
    assert "0 of 0 sites have current paired evidence." in text
    assert "No sites tracked. Add the first combined observation to begin monitoring." in text
    assert "Monitoring is complete" not in text
    assert 'href="#snapshot"' in section
