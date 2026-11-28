"""HEAD-based monitors must receive the same health decisions as GET probes."""
import importlib
import sqlite3
import warnings
from unittest.mock import Mock

import pytest


@pytest.fixture
def probe_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "probes.sqlite3"))
    monkeypatch.setenv("WP_FLEET_OPS_REVISION", "probe-test-revision")
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main


@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_head_probe_matches_get_status_and_headers_without_body(probe_client, path):
    client, main = probe_client
    main.store.upsert_site("Probe Site", "https://probe.example", "Care Client")
    before = (main.store.list_sites(), main.store.latest_dashboard(), main.store.latest_care_checks())

    get = client.get(path)
    head = client.head(path)

    assert get.status_code == head.status_code == 200
    assert get.json()["revision"] == "probe-test-revision"
    assert head.content == b""
    for header in ("content-type", "content-length", "cache-control", "content-security-policy",
                   "x-content-type-options", "x-frame-options", "referrer-policy"):
        assert head.headers[header] == get.headers[header]
    assert head.headers["cache-control"] == "no-store"
    assert (main.store.list_sites(), main.store.latest_dashboard(), main.store.latest_care_checks()) == before


@pytest.mark.parametrize("message", ["database is locked", "attempt to write a readonly database"])
def test_head_readiness_preserves_database_failure_without_affecting_liveness(
    probe_client, monkeypatch, message,
):
    client, main = probe_client
    health_counts = Mock(side_effect=sqlite3.OperationalError(message))
    monkeypatch.setattr(main.store, "health_counts", health_counts)

    get = client.get("/ready")
    head = client.head("/ready")

    assert get.status_code == head.status_code == 503
    assert get.json()["status"] == "not_ready"
    assert get.json()["database"] == "unavailable"
    assert message not in get.text
    assert head.content == b""
    assert head.headers["content-length"] == get.headers["content-length"]
    assert head.headers["cache-control"] == "no-store"
    assert health_counts.call_count == 2, "HEAD must execute the existing database readiness check."
    assert client.get("/health").status_code == client.head("/health").status_code == 200
    assert health_counts.call_count == 2, "Liveness must remain independent of database health."


@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_probe_head_support_does_not_enable_mutations_or_change_openapi(probe_client, path):
    client, _main = probe_client
    assert client.post(path).status_code == 405
    assert client.delete(path).status_code == 405
    assert set(client.get("/openapi.json").json()["paths"][path]) == {"get"}
