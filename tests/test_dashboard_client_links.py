"""Observation rows should open the exact account's existing scoped report."""
import importlib
import warnings
from contextlib import closing
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture
def link_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "client-links.sqlite3"))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main


class AccountLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.tags = []
        self._table = None
        self._link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append(tag)
        if tag == "table":
            self._table = {"attrs": attrs, "links": []}
            self.tables.append(self._table)
        elif tag == "a" and self._table is not None:
            self._link = {"attrs": attrs, "text": ""}
            self._table["links"].append(self._link)

    def handle_data(self, data):
        if self._link is not None:
            self._link["text"] += data

    def handle_endtag(self, tag):
        if tag == "a":
            self._link = None
        elif tag == "table":
            self._table = None


def capture(client, name, url, account):
    response = client.post("/snapshot", data={
        "name": name, "url": url, "client": account, "uptime_ok": "true",
        "ssl_days": 90, "wp_updates": 0, "backup_age_hours": 12,
        "response_ms": 100, "security_header_count": 3,
    }, follow_redirects=False)
    assert response.status_code == 303


@pytest.mark.parametrize("account", [
    "Care Client", 'Church & Mission + East /? #1 = "Care" <ops>', "Église 東京", "",
])
@pytest.mark.parametrize("state", ["current", "stale_snapshot", "stale_care"])
def test_observation_account_links_preserve_scope_and_evidence(link_client, account, state):
    client, main = link_client
    capture(client, "Selected Site", "https://selected-link.example", account)
    capture(client, "Outside Site", "https://outside-link.example", "Outside Client")
    main.store.upsert_site("Registry Only", "https://registry-link.example", "Registry Client")
    if state != "current":
        table, timestamp = (("snapshots", "captured_at") if state == "stale_snapshot"
                            else ("care_checks", "checked_at"))
        with closing(main.store._connect()) as connection, connection:
            connection.execute(
                f"update {table} set {timestamp} = '2000-01-01 00:00:00' "
                "where site_id = (select id from sites where url = ?)",
                ("https://selected-link.example",),
            )
    before = (main.store.list_sites(), main.store.latest_dashboard(), main.store.latest_care_checks())
    summary = client.get("/api/summary").json()
    response = client.get("/")
    assert response.status_code == 200
    parser = AccountLinkParser()
    parser.feed(response.text)
    assert len(parser.tables) == 2
    label = account or "Unassigned"
    for table in parser.tables:
        assert len(table["links"]) == 2, "Every observed row should link its account, not its site."
        selected = next(link for link in table["links"] if link["text"] == label)
        attrs = selected["attrs"]
        assert "client-report-link" in attrs.get("class", "").split()
        assert attrs.get("aria-label") == f"Open client report for {label}"
        assert "target" not in attrs and "onclick" not in attrs
        target = urlsplit(attrs["href"])
        assert not target.scheme and not target.netloc and not target.fragment
        assert target.path == "/report"
        assert parse_qs(target.query, strict_parsing=True) == {"client": [label]}
        report = client.get(attrs["href"])
        assert report.status_code == 200
        assert "Selected Site" in report.text
        assert "Outside Site" not in report.text and "Registry Only" not in report.text
        assert f"Sites reviewed: {1 if state == 'current' else 0}" in report.text
        if state == "current":
            assert "Monitoring gaps:" not in report.text
        else:
            assert "Monitoring gaps: 1" in report.text
    assert "ops" not in parser.tags, "Account labels must not become markup."
    assert ".client-report-link{text-decoration:underline;" in response.text
    assert 'href="/report"' in response.text, "Keep the full-fleet report available."
    assert (main.store.list_sites(), main.store.latest_dashboard(), main.store.latest_care_checks()) == before
    after = client.get("/api/summary").json()
    assert {key: value for key, value in summary.items() if key != "generated_at"} == {
        key: value for key, value in after.items() if key != "generated_at"
    }


def test_empty_inventory_does_not_offer_observation_account_links(link_client):
    client, main = link_client
    response = client.get("/")
    assert response.status_code == 200
    parser = AccountLinkParser()
    parser.feed(response.text)
    assert parser.tables == []
    assert 'class="client-report-link"' not in response.text
    assert 'href="/report"' in response.text
    assert main.store.list_sites() == []
