"""Headline metric descriptions must match paired findings, not just scores."""
import importlib
import warnings
from contextlib import closing
from html.parser import HTMLParser

import pytest


@pytest.fixture
def metrics_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "metrics.sqlite3"))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main


class MetricParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metrics = {}
        self._metric = None
        self._field = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and "metric" in (attrs.get("class") or "").split():
            self._metric = {"label": "", "strong": "", "small": ""}
        elif self._metric is not None and tag in {"label", "strong", "small"}:
            self._field = tag

    def handle_data(self, data):
        if self._metric is not None and self._field is not None:
            self._metric[self._field] += data

    def handle_endtag(self, tag):
        if self._metric is not None and tag == "div":
            self.metrics[self._metric["label"]] = self._metric
            self._metric = None
        if tag == self._field:
            self._field = None


def capture(client, name="Metric Site", updates=0):
    response = client.post("/snapshot", data={
        "name": name, "url": f"https://{name.lower().replace(' ', '-')}.example",
        "client": "Shared Client", "uptime_ok": "true", "ssl_days": 90,
        "wp_updates": updates, "backup_age_hours": 12,
        "response_ms": 100, "security_header_count": 3,
    }, follow_redirects=False)
    assert response.status_code == 303


def assert_metrics(client, main, expected):
    before = (main.store.latest_dashboard(), main.store.latest_care_checks())
    summary = client.get("/api/summary").json()
    response = client.get("/")
    assert response.status_code == 200
    parser = MetricParser()
    parser.feed(response.text)
    contracts = (
        ("Current risks", "needs_attention", "sites with current critical findings"),
        ("Current healthy sites", "healthy_sites", "healthy paired care and fleet evidence"),
        ("Current care risks", "client_risks", "sites with current critical care findings"),
    )
    for (label, key, description), count in zip(contracts, expected, strict=True):
        assert summary[key] == count
        assert label in parser.metrics, f"Missing site-based metric: {label}"
        metric = parser.metrics[label]
        assert metric["strong"] == str(count)
        assert metric["small"] == description
    assert "Current client risks" not in parser.metrics
    assert "current sites below 65 score" not in response.text
    assert "current score 85 or higher" not in response.text
    assert (main.store.latest_dashboard(), main.store.latest_care_checks()) == before


@pytest.mark.parametrize("updates,expected", [
    (0, (0, 1, 0)), (1, (0, 0, 0)), (5, (1, 0, 0)),
])
def test_metrics_describe_alert_aware_paired_counts(metrics_client, updates, expected):
    client, main = metrics_client
    capture(client, updates=updates)
    # A warning or critical alert can exclude a high-scoring site from healthy.
    assert main.store.latest_dashboard()[0]["score"] >= 85
    assert_metrics(client, main, expected)


@pytest.mark.parametrize("score,saved_status,expected", [
    (40, "green", (1, 0, 1)),
    (100, "red", (1, 0, 1)),
    (75, "green", (0, 0, 0)),
    (100, "yellow", (0, 0, 0)),
])
def test_metrics_describe_corrected_and_retained_care_findings(
    metrics_client, score, saved_status, expected,
):
    client, main = metrics_client
    capture(client)
    with closing(main.store._connect()) as connection, connection:
        connection.execute("update care_checks set score = ?, status = ?", (score, saved_status))
    assert main.store.latest_dashboard()[0]["score"] == 100
    assert_metrics(client, main, expected)


@pytest.mark.parametrize("table,timestamp", [
    ("snapshots", "captured_at"), ("care_checks", "checked_at"),
])
@pytest.mark.parametrize("value", ["2000-01-01 00:00:00", "invalid", "9999-01-01 00:00:00", None])
def test_metric_descriptions_preserve_incomplete_evidence_counts(metrics_client, table, timestamp, value):
    client, main = metrics_client
    capture(client, updates=5)
    with closing(main.store._connect()) as connection, connection:
        if value is None:
            connection.execute(f"delete from {table}")
        else:
            connection.execute(f"update {table} set {timestamp} = ?", (value,))
    assert_metrics(client, main, (0, 0, 0))


def test_care_risk_metric_counts_sites_not_distinct_clients(metrics_client):
    client, main = metrics_client
    capture(client, name="First Site")
    capture(client, name="Second Site")
    with closing(main.store._connect()) as connection, connection:
        connection.execute("update care_checks set status = 'red'")
    assert {site["client"] for site in main.store.list_sites()} == {"Shared Client"}
    assert_metrics(client, main, (2, 0, 2))


def test_metric_descriptions_preserve_empty_and_registry_only_counts(metrics_client):
    client, main = metrics_client
    assert_metrics(client, main, (0, 0, 0))
    main.store.upsert_site("New Site", "https://new-metric.example", "Shared Client")
    assert_metrics(client, main, (0, 0, 0))
