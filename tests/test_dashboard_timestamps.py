"""Dashboard rows expose their own recorded values without changing evidence."""
import importlib
import warnings
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import pytest


@pytest.fixture
def timestamp_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "timestamps.sqlite3"))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main


class ObservationTableParser(HTMLParser):
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
            self.table = {"headers": [], "rows": []}
        elif self.table is not None:
            if tag == "tr":
                self.row = []
            elif tag in ("th", "td"):
                self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if self.table is None:
            return
        if tag in ("th", "td") and self.cell is not None:
            text = "".join(self.cell).strip()
            if tag == "th":
                self.table["headers"].append(text)
            else:
                assert self.row is not None
                self.row.append(text)
            self.cell = None
        elif tag == "tr" and self.row:
            self.table["rows"].append(self.row)
            self.row = None
        elif tag == "table":
            self.tables.append(self.table)
            self.table = None


def capture(client):
    response = client.post("/snapshot", data={
        "name": "Timestamp Site", "url": "https://timestamp.example",
        "client": "Care Client", "uptime_ok": "true", "ssl_days": 90,
        "wp_updates": 0, "backup_age_hours": 12,
        "response_ms": 100, "security_header_count": 3,
    }, follow_redirects=False)
    assert response.status_code == 303


@pytest.mark.parametrize("source", ["snapshot", "care"])
@pytest.mark.parametrize("state", ["current", "stale", "clock_skew", "invalid", "blank"])
def test_dashboard_tables_show_each_sources_exact_recorded_timestamp(
    timestamp_client, source, state,
):
    client, main = timestamp_client
    capture(client)
    now = datetime.now(timezone.utc)
    fleet_time = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    care_time = (now - timedelta(hours=1)).astimezone(
        timezone(timedelta(hours=-5)),
    ).isoformat(timespec="seconds")
    if state == "current":
        value = fleet_time if source == "snapshot" else care_time
    elif state == "stale":
        value = "2000-01-01 00:00:00"
    elif state == "clock_skew":
        value = "9999-01-01 00:00:00"
    elif state == "invalid":
        value = '<script>alert("timestamp")</script>'
    else:
        value = ""
    if source == "snapshot":
        fleet_time = value
    else:
        care_time = value
    with closing(main.store._connect()) as connection, connection:
        connection.execute("update snapshots set captured_at = ?", (fleet_time,))
        connection.execute("update care_checks set checked_at = ?", (care_time,))
    before = (main.store.latest_dashboard(), main.store.latest_care_checks())
    summary = client.get("/api/summary").json()

    response = client.get("/")
    assert response.status_code == 200
    parser = ObservationTableParser()
    parser.feed(response.text)
    assert len(parser.tables) == 2
    for table, expected in zip(parser.tables, (fleet_time, care_time), strict=True):
        assert "Recorded" in table["headers"]
        assert len(table["rows"]) == 1
        row = table["rows"][0]
        assert len(row) == len(table["headers"])
        assert row[table["headers"].index("Recorded")] == (expected or "Unavailable")
    assert parser.script_count == 0
    if state == "invalid":
        assert value not in response.text
        assert "&lt;script&gt;" in response.text
    assert summary["current_evidence_count"] == (1 if state == "current" else 0)
    assert summary["monitoring_gap_count"] == (0 if state == "current" else 1)
    after_summary = client.get("/api/summary").json()
    assert {key: value for key, value in after_summary.items() if key != "generated_at"} == {
        key: value for key, value in summary.items() if key != "generated_at"
    }
    assert (main.store.latest_dashboard(), main.store.latest_care_checks()) == before


@pytest.mark.parametrize("latency_ms", [0, 1732, (1 << 63) - 1])
@pytest.mark.parametrize("historical_source", [None, "care", "snapshot"])
def test_care_table_shows_its_recorded_response_time_without_changing_evidence(
    timestamp_client, latency_ms, historical_source,
):
    client, main = timestamp_client
    capture(client)
    # Imported paired observations can disagree; do not display fleet latency
    # as if it were the separately recorded care-check measurement.
    with closing(main.store._connect()) as connection, connection:
        connection.execute("update care_checks set latency_ms = ?", (latency_ms,))
        if historical_source == "care":
            connection.execute("update care_checks set checked_at = '2000-01-01 00:00:00'")
        elif historical_source == "snapshot":
            connection.execute("update snapshots set captured_at = '2000-01-01 00:00:00'")
    before = (main.store.latest_dashboard(), main.store.latest_care_checks())
    summary = client.get("/api/summary").json()

    response = client.get("/")
    assert response.status_code == 200
    parser = ObservationTableParser()
    parser.feed(response.text)
    assert len(parser.tables) == 2
    fleet, care = parser.tables
    for table in (fleet, care):
        assert table["headers"].count("Response") == 1
        assert len(table["rows"]) == 1
        assert len(table["rows"][0]) == len(table["headers"])
    assert fleet["rows"][0][fleet["headers"].index("Response")] == "100ms"
    assert care["rows"][0][care["headers"].index("Response")] == f"{latency_ms}ms"
    evidence = care["rows"][0][care["headers"].index("Evidence")]
    assert evidence == ("current" if historical_source is None else f"unknown · {historical_source} stale")
    assert (main.store.latest_dashboard(), main.store.latest_care_checks()) == before
    after = client.get("/api/summary").json()
    assert {key: value for key, value in after.items() if key != "generated_at"} == {
        key: value for key, value in summary.items() if key != "generated_at"
    }


@pytest.mark.parametrize("historical_source", [None, "care", "snapshot"])
@pytest.mark.parametrize("version, expected", [
    ("6.6.2", "6.6.2"),
    ("  6.6.2  ", "6.6.2"),
    ("", "unknown"),
    ("6.6.2 & preview", "6.6.2 & preview"),
    ('<script>alert("version")</script>', '<script>alert("version")</script>'),
])
def test_care_table_shows_recorded_wordpress_version_without_changing_evidence(
    timestamp_client, version, expected, historical_source,
):
    client, main = timestamp_client
    response = client.post("/care/manual-check", data={
        "name": "Version Site", "url": "https://version.example",
        "client": "Care Client", "http_status": 200, "latency_ms": 100,
        "ssl_days_remaining": 90, "wordpress_version": version,
        "update_count": 0, "backup_age_hours": 12,
    }, follow_redirects=False)
    assert response.status_code == 303
    with closing(main.store._connect()) as connection, connection:
        if historical_source == "care":
            connection.execute("update care_checks set checked_at = '2000-01-01 00:00:00'")
        elif historical_source == "snapshot":
            connection.execute("update snapshots set captured_at = '2000-01-01 00:00:00'")
    before = (main.store.latest_dashboard(), main.store.latest_care_checks())
    assert before[1][0]["wordpress_version"] == expected
    summary = client.get("/api/summary").json()

    response = client.get("/")
    assert response.status_code == 200
    parser = ObservationTableParser()
    parser.feed(response.text)
    assert len(parser.tables) == 2
    fleet, care = parser.tables
    assert "WP version" not in fleet["headers"]
    assert care["headers"].count("WP version") == 1
    assert len(care["rows"]) == 1
    row = care["rows"][0]
    assert len(row) == len(care["headers"])
    assert row[care["headers"].index("WP version")] == expected
    assert row[care["headers"].index("Evidence")] == (
        "current" if historical_source is None else f"unknown · {historical_source} stale"
    )
    assert parser.script_count == 0
    if "<script>" in expected:
        assert expected not in response.text
        assert "&lt;script&gt;" in response.text
    if "&" in expected:
        assert "6.6.2 &amp; preview" in response.text
    assert (main.store.latest_dashboard(), main.store.latest_care_checks()) == before
    after = client.get("/api/summary").json()
    assert {key: value for key, value in after.items() if key != "generated_at"} == {
        key: value for key, value in summary.items() if key != "generated_at"
    }


@pytest.mark.parametrize("historical_source", [None, "care", "snapshot"])
@pytest.mark.parametrize("http_status", [0, 200, 204, 206, 301, 503])
def test_care_table_explains_no_response_without_reinterpreting_http_codes(
    timestamp_client, http_status, historical_source,
):
    client, main = timestamp_client
    response = client.post("/care/manual-check", data={
        "name": "HTTP Evidence", "url": "https://http-evidence.example",
        "client": "Care Client", "http_status": http_status, "latency_ms": 100,
        "ssl_days_remaining": 90, "wordpress_version": "unknown",
        "update_count": 0, "backup_age_hours": 12,
    }, follow_redirects=False)
    assert response.status_code == 303
    with closing(main.store._connect()) as connection, connection:
        if historical_source == "care":
            connection.execute("update care_checks set checked_at = '2000-01-01 00:00:00'")
        elif historical_source == "snapshot":
            connection.execute("update snapshots set captured_at = '2000-01-01 00:00:00'")
    before = (main.store.latest_dashboard(), main.store.latest_care_checks())
    assert before[1][0]["http_status"] == http_status
    summary = client.get("/api/summary").json()

    response = client.get("/")
    assert response.status_code == 200
    parser = ObservationTableParser()
    parser.feed(response.text)
    assert len(parser.tables) == 2
    care = parser.tables[1]
    assert care["headers"].count("HTTP") == 1
    assert len(care["rows"]) == 1
    row = care["rows"][0]
    assert len(row) == len(care["headers"])
    assert row[care["headers"].index("HTTP")] == (
        "No response (0)" if http_status == 0 else str(http_status)
    )
    assert row[care["headers"].index("Evidence")] == (
        "current" if historical_source is None else f"unknown · {historical_source} stale"
    )
    assert summary["current_evidence_count"] == (1 if historical_source is None else 0)
    assert (main.store.latest_dashboard(), main.store.latest_care_checks()) == before
    after = client.get("/api/summary").json()
    assert {key: value for key, value in after.items() if key != "generated_at"} == {
        key: value for key, value in summary.items() if key != "generated_at"
    }


@pytest.mark.parametrize("header_count", [0, 1, 2, 3])
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("historical_source", [None, "care", "snapshot"])
def test_fleet_table_shows_recorded_header_count_without_claiming_coverage(
    timestamp_client, header_count, scheme, historical_source,
):
    client, main = timestamp_client
    response = client.post("/snapshot", data={
        "name": "Header Evidence", "url": f"{scheme}://headers.example",
        "client": "Care Client", "uptime_ok": "true", "ssl_days": 90,
        "wp_updates": 0, "backup_age_hours": 12,
        "response_ms": 100, "security_header_count": header_count,
    }, follow_redirects=False)
    assert response.status_code == 303
    with closing(main.store._connect()) as connection, connection:
        if historical_source == "care":
            connection.execute("update care_checks set checked_at = '2000-01-01 00:00:00'")
        elif historical_source == "snapshot":
            connection.execute("update snapshots set captured_at = '2000-01-01 00:00:00'")
    before = (main.store.latest_dashboard(), main.store.latest_care_checks())
    assert before[0][0]["security_header_count"] == header_count
    summary = client.get("/api/summary").json()

    response = client.get("/")
    assert response.status_code == 200
    parser = ObservationTableParser()
    parser.feed(response.text)
    assert len(parser.tables) == 2
    fleet, care = parser.tables
    assert fleet["headers"].count("Recorded headers") == 1
    assert "Recorded headers" not in care["headers"]
    assert len(fleet["rows"]) == 1
    row = fleet["rows"][0]
    assert len(row) == len(fleet["headers"])
    assert row[fleet["headers"].index("Recorded headers")] == f"{header_count} of 3"
    assert row[fleet["headers"].index("Evidence")] == (
        "current" if historical_source is None else f"unknown · {historical_source} stale"
    )
    # A count alone must not assert coverage, especially for HTTP or old data.
    assert f'<td>{header_count} of 3</td>' in response.text
    assert summary["current_evidence_count"] == (1 if historical_source is None else 0)
    assert (main.store.latest_dashboard(), main.store.latest_care_checks()) == before
    after = client.get("/api/summary").json()
    assert {key: value for key, value in after.items() if key != "generated_at"} == {
        key: value for key, value in summary.items() if key != "generated_at"
    }


def test_dashboard_timestamp_columns_preserve_empty_table_states(timestamp_client):
    client, _ = timestamp_client
    response = client.get("/")
    assert response.status_code == 200
    parser = ObservationTableParser()
    parser.feed(response.text)
    assert parser.tables == []
    assert "No fleet snapshots yet." in response.text
    assert "No care checks yet." in response.text
