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


class GapParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = {}
        self.current_url = None

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.current_url = dict(attrs).get("data-site-url")
            if self.current_url is not None:
                self.rows[self.current_url] = []

    def handle_data(self, data):
        if self.current_url is not None:
            self.rows[self.current_url].append(data)

    def handle_endtag(self, tag):
        if tag == "li":
            self.current_url = None


def dashboard_gap_rows(client):
    section, _ = evidence_section(client)
    parser = GapParser()
    parser.feed(section)
    return {
        url: " ".join(" ".join(parts).split())
        for url, parts in parser.rows.items()
    }


@pytest.mark.parametrize("table, timestamp, source", [
    ("snapshots", "captured_at", "Fleet snapshot"),
    ("care_checks", "checked_at", "Care check"),
])
@pytest.mark.parametrize("value, freshness", [
    ("2000-01-01 00:00:00", "stale"), ("invalid timestamp", "invalid"),
    ("9999-01-01 00:00:00", "clock_skew"), (None, "missing"),
])
def test_dashboard_lists_each_gap_and_its_minimum_repair(
    coverage_client, table, timestamp, source, value, freshness,
):
    client, main = coverage_client
    with closing(main.store._connect()) as connection, connection:
        if value is None:
            connection.execute(f"delete from {table} where site_id = 1")
        else:
            connection.execute(f"update {table} set {timestamp} = ? where site_id = 1", (value,))
    rows = dashboard_gap_rows(client)
    assert list(rows) == ["https://first.example"]
    text = rows["https://first.example"]
    assert "first" in text
    assert "Care Client" in text
    assert "https://first.example" in text
    assert f"{source}: {freshness.replace('_', ' ')}" in text
    other_source = "Care check" if table == "snapshots" else "Fleet snapshot"
    assert f"{other_source}: current" in text
    action = main._monitoring_coverage_action(
        freshness if table == "snapshots" else "current",
        freshness if table == "care_checks" else "current",
    )
    assert action in text
    section, _ = evidence_section(client)
    assert '<details id="monitoring-gaps">' in section
    assert "Review 1 site with evidence gaps" in section


def test_dashboard_gap_list_includes_registry_only_sites_and_escapes_labels(coverage_client):
    client, main = coverage_client
    main.store.upsert_site('<img src=x onerror=alert(1)>', 'https://third.example?a=1&b=2', '<script>bad()</script>')
    main.store.upsert_site("Unassigned site", "https://fourth.example", "")
    rows = dashboard_gap_rows(client)
    assert set(rows) == {"https://third.example?a=1&b=2", "https://fourth.example"}
    for text in rows.values():
        assert "Fleet snapshot: missing" in text
        assert "Care check: missing" in text
        assert "Capture an initial combined care check and fleet snapshot." in text
    assert "Unassigned" in rows["https://fourth.example"]
    section, _ = evidence_section(client)
    assert "Review 2 sites with evidence gaps" in section
    assert "&lt;img src=x onerror=alert(1)&gt;" in section
    assert "&lt;script&gt;bad()&lt;/script&gt;" in section
    assert "<img" not in section
    assert "<script" not in section


def test_dashboard_gap_list_reuses_existing_inventory_reads(coverage_client, monkeypatch):
    client, main = coverage_client
    main.store.upsert_site("New site", "https://third.example", "Care Client")
    calls = {"list_sites": 0, "latest_dashboard": 0, "latest_care_checks": 0}
    for method in calls:
        original = getattr(main.store, method)

        def counted(*args, _method=method, _original=original, **kwargs):
            calls[_method] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(main.store, method, counted)
    rows = dashboard_gap_rows(client)
    assert list(rows) == ["https://third.example"]
    assert calls == {method: 1 for method in calls}


def test_dashboard_omits_gap_list_when_paired_evidence_is_complete(coverage_client):
    client, main = coverage_client
    section, _ = evidence_section(client)
    assert 'id="monitoring-gaps"' not in section
    assert dashboard_gap_rows(client) == {}
    for site in main.store.list_sites():
        main.store.delete_site(site["url"])
    section, _ = evidence_section(client)
    assert 'id="monitoring-gaps"' not in section
