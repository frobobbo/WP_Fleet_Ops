"""Database operations must release connections without waiting for GC."""
from contextlib import closing
import sqlite3

import pytest

from wp_fleet_ops.checks import evaluate_site
from wp_fleet_ops.fleet import FleetSite, calculate_health_score, generate_alerts
from wp_fleet_ops.storage import FleetOpsStore


def assert_closed(connection):
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("select 1")


def test_store_closes_initialization_connection(tmp_path, monkeypatch):
    original_connect = sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    try:
        FleetOpsStore(tmp_path / "fleetops.sqlite3")
        assert len(connections) == 1
        assert_closed(connections[0])
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize(
    "operation",
    [
        "upsert_site", "list_sites", "health_counts", "save_care_check",
        "latest_care_checks", "recent_care_checks", "count_care_checks",
        "save_snapshot", "save_observation", "latest_dashboard",
        "recent_snapshots", "count_snapshots", "recent_trend_snapshots",
        "site_snapshots", "count_site_snapshots", "delete_site",
    ],
)
def test_store_closes_operation_connection(tmp_path, monkeypatch, operation):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")
    name, url = "Connection Test", "https://connection.example"
    site_id = store.upsert_site(name, url, "Test Client")
    check = evaluate_site(name, url, 200, 200, 60, "unknown", 0, 24)
    site = FleetSite(name, url, True, 60, 0, 24, 200, 3)
    score, alerts = calculate_health_score(site), generate_alerts(site)
    store.save_care_check(site_id, check)
    store.save_snapshot(site_id, site, score, alerts)
    args = {
        "upsert_site": ("Updated Name", url),
        "save_care_check": (site_id, check),
        "save_snapshot": (site_id, site, score, alerts),
        "save_observation": (name, url, "Test Client", site, score, alerts, check),
        "site_snapshots": (url,),
        "count_site_snapshots": (url,),
        "delete_site": (url,),
    }.get(operation, ())
    with closing(store._connect()) as connection:
        monkeypatch.setattr(store, "_connect", lambda: connection)
        getattr(store, operation)(*args)
        assert_closed(connection)

    # Connection cleanup must not discard a successful write transaction.
    with closing(sqlite3.connect(store.path)) as persisted:
        assert persisted.execute("select count(*) from sites").fetchone()[0] == (
            0 if operation == "delete_site" else 1
        )
        if operation == "upsert_site":
            assert persisted.execute("select name from sites").fetchone()[0] == "Updated Name"
        expected = 2 if operation in ("save_care_check", "save_observation") else 1
        assert persisted.execute("select count(*) from care_checks").fetchone()[0] == (
            0 if operation == "delete_site" else expected
        )
        expected = 2 if operation in ("save_snapshot", "save_observation") else 1
        assert persisted.execute("select count(*) from snapshots").fetchone()[0] == (
            0 if operation == "delete_site" else expected
        )


@pytest.mark.parametrize("deferred_constraint", [False, True], ids=["statement-failure", "commit-failure"])
def test_store_closes_connection_and_rolls_back_failed_write(tmp_path, monkeypatch, deferred_constraint):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")
    check = evaluate_site("Missing", "https://missing.example", 200, 200, 60, "unknown", 0, 24)
    with closing(store._connect()) as connection:
        if deferred_constraint:
            connection.execute("pragma defer_foreign_keys = on")
        monkeypatch.setattr(store, "_connect", lambda: connection)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            store.save_care_check(999, check)
        assert_closed(connection)

    with closing(sqlite3.connect(store.path)) as persisted:
        assert persisted.execute("select count(*) from care_checks").fetchone()[0] == 0


def test_store_closes_connection_after_read_failure(tmp_path, monkeypatch):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")
    with closing(store._connect()) as connection:
        connection.set_authorizer(lambda *_args: sqlite3.SQLITE_DENY)
        monkeypatch.setattr(store, "_connect", lambda: connection)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            store.list_sites()
        assert_closed(connection)
