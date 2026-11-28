"""Observation rows identify endpoints even when site names are identical."""
import importlib
import warnings
from contextlib import closing
from html.parser import HTMLParser

import pytest


@pytest.fixture
def site_url_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "site-urls.sqlite3"))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main


class SiteIdentityParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None
        self.script_count = 0

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.script_count += 1
        if tag == "table":
            self.table = []
        elif self.table is not None:
            if tag == "tr":
                self.row = []
            elif tag == "td" or tag == "th" and dict(attrs).get("scope") == "row":
                self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if self.table is None:
            return
        if tag in {"td", "th"} and self.cell is not None:
            assert self.row is not None
            self.row.append("".join(self.cell))
            self.cell = None
        elif tag == "tr" and self.row:
            self.table.append(self.row)
            self.row = None
        elif tag == "table":
            self.tables.append(self.table)
            self.table = None


@pytest.mark.parametrize("historical_source", [None, "snapshot", "care"])
def test_both_observation_tables_identify_same_named_sites_without_changing_evidence(
    site_url_client, historical_source,
):
    client, main = site_url_client
    name = '<script>alert("site")</script>'
    urls = (
        "https://primary.example/wordpress?account=one&view=care",
        "https://secondary.example/wordpress?account=two&view=fleet",
    )
    for url in urls:
        response = client.post("/snapshot", data={
            "name": name, "url": url, "client": "Care Client",
            "uptime_ok": "true", "ssl_days": 90, "wp_updates": 0,
            "backup_age_hours": 12, "response_ms": 100,
            "security_header_count": 3,
        }, follow_redirects=False)
        assert response.status_code == 303
    if historical_source:
        table, field = (
            ("snapshots", "captured_at") if historical_source == "snapshot"
            else ("care_checks", "checked_at")
        )
        with closing(main.store._connect()) as connection, connection:
            connection.execute(f"update {table} set {field} = '2000-01-01 00:00:00'")
    before = (main.store.latest_dashboard(), main.store.latest_care_checks())
    summary = client.get("/api/summary").json()
    response = client.get("/")
    assert response.status_code == 200
    parser = SiteIdentityParser()
    parser.feed(response.text)
    assert len(parser.tables) == 2
    for table in parser.tables:
        assert len(table) == 2
        for url in urls:
            identity_cells = [row[0] for row in table if url in row[0]]
            assert len(identity_cells) == 1
            assert name in identity_cells[0]
    assert parser.script_count == 0
    assert name not in response.text
    assert "&lt;script&gt;" in response.text
    assert "&amp;view=" in response.text
    assert (main.store.latest_dashboard(), main.store.latest_care_checks()) == before
    after = client.get("/api/summary").json()
    assert {k: v for k, v in after.items() if k != "generated_at"} == {
        k: v for k, v in summary.items() if k != "generated_at"
    }


def test_site_identity_presentation_preserves_empty_observation_states(site_url_client):
    client, _ = site_url_client
    response = client.get("/")
    assert response.status_code == 200
    parser = SiteIdentityParser()
    parser.feed(response.text)
    assert parser.tables == []
    assert "No fleet snapshots yet." in response.text
    assert "No care checks yet." in response.text
