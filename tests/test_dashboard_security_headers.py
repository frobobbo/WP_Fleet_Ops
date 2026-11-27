"""Named care-header evidence must not be confused with count-only coverage."""
import importlib
import json
import warnings
from contextlib import closing
from html.parser import HTMLParser

import pytest


@pytest.fixture
def header_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "headers.sqlite3"))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main


class CareHeaderParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.column = None
        self.headers = []
        self.cells = []
        self.cell_attrs = []
        self.script_count = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.script_count += 1
        if tag == "table" and attrs.get("aria-labelledby") == "care-table-heading":
            self.in_table = True
        elif self.in_table and tag in {"th", "td"}:
            self.column = tag
            if tag == "th":
                self.headers.append("")
            else:
                self.cells.append("")
                self.cell_attrs.append([])
        elif self.in_table and self.column == "td":
            self.cell_attrs[-1].append(attrs)

    def handle_data(self, data):
        if self.in_table and self.column == "th":
            self.headers[-1] += data
        elif self.in_table and self.column == "td":
            self.cells[-1] += data

    def handle_endtag(self, tag):
        if tag in {"th", "td"}:
            self.column = None
        elif tag == "table":
            self.in_table = False


def header_cell(client):
    response = client.get("/")
    assert response.status_code == 200
    parser = CareHeaderParser()
    parser.feed(response.text)
    assert "Named headers" in parser.headers, "Care rows need their saved named-header evidence."
    index = parser.headers.index("Named headers")
    assert len(parser.cells) == len(parser.headers)
    assert not any(
        {"green", "yellow", "red"}.intersection(attrs.get("class", "").split())
        for attrs in parser.cell_attrs[index]
    ), "Recorded header names are neutral evidence, not a current coverage badge."
    return response.text, parser, " ".join(parser.cells[index].split())


@pytest.mark.parametrize("headers", [
    {},
    {"strict-transport-security": "max-age=31536000"},
    {"x-frame-options": "DENY", "content-security-policy": "frame-ancestors 'none'"},
    {"strict-transport-security": "max-age=31536000", "x-frame-options": "DENY",
     "content-security-policy": "frame-ancestors 'none'"},
])
@pytest.mark.parametrize("historical_source", [None, "snapshot", "care"])
def test_live_care_rows_show_named_headers_without_rewriting_health(
    header_client, monkeypatch, headers, historical_source,
):
    client, main = header_client
    monkeypatch.setattr(main, "fetch_basic_site_check", lambda name, url, **kwargs: main.evaluate_site(
        name, url, 200, 100, 90, "6.6.2", 0, 12, headers,
    ))
    saved = client.post("/care/fetch-check", data={
        "name": "Header Evidence", "url": "https://headers.example", "client": "Care Client",
    }, follow_redirects=False)
    assert saved.status_code == 303
    if historical_source:
        table, field = (("snapshots", "captured_at") if historical_source == "snapshot"
                        else ("care_checks", "checked_at"))
        with closing(main.store._connect()) as connection, connection:
            connection.execute(f"update {table} set {field} = '2000-01-01 00:00:00'")
    before = (main.store.latest_dashboard(), main.store.latest_care_checks())
    summary = client.get("/api/summary").json()
    html, _, text = header_cell(client)
    recorded = before[1][0]["security_headers"]
    names = sorted(recorded)
    expected = " ".join(names) if names else "No named header evidence."
    if historical_source:
        expected = "observed headers " + expected
    assert text == expected
    assert "Named headers are recorded evidence, not a current security-coverage assessment." in html
    assert (main.store.latest_dashboard(), main.store.latest_care_checks()) == before
    after = client.get("/api/summary").json()
    assert {k: v for k, v in after.items() if k != "generated_at"} == {
        k: v for k, v in summary.items() if k != "generated_at"
    }


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_count_only_snapshots_do_not_invent_named_header_evidence(header_client, count):
    client, main = header_client
    response = client.post("/snapshot", data={
        "name": "Count Only", "url": "https://count-only.example", "uptime_ok": "true",
        "ssl_days": 90, "wp_updates": 0, "backup_age_hours": 12, "response_ms": 100,
        "security_header_count": count,
    }, follow_redirects=False)
    assert response.status_code == 303
    _, _, text = header_cell(client)
    assert text == "No named header evidence."
    assert main.store.latest_dashboard()[0]["security_header_count"] == count
    assert main.store.latest_care_checks()[0]["security_headers"] == {}


def test_named_header_labels_are_html_escaped_and_values_are_not_published(header_client):
    client, main = header_client
    assert client.post("/care/manual-check", data={
        "name": "Imported Headers", "url": "https://imported-headers.example",
    }, follow_redirects=False).status_code == 303
    name = '<script>alert("header")</script>'
    with closing(main.store._connect()) as connection, connection:
        row_id, raw = connection.execute("select id, raw_json from care_checks").fetchone()
        data = json.loads(raw)
        data["security_headers"] = {name: "private-imported-header-value"}
        connection.execute("update care_checks set raw_json = ? where id = ?", (json.dumps(data), row_id))
    html, parser, text = header_cell(client)
    assert text == name
    assert name not in html
    assert "&lt;script&gt;" in html
    assert "private-imported-header-value" not in html
    assert parser.script_count == 0


def test_empty_dashboard_does_not_add_header_evidence_rows(header_client):
    client, main = header_client
    response = client.get("/")
    parser = CareHeaderParser()
    parser.feed(response.text)
    assert response.status_code == 200
    assert parser.headers == parser.cells == []
    assert "No care checks yet." in response.text
    assert main.store.health_counts() == {"sites": 0, "care_checks": 0, "fleet_snapshots": 0}
