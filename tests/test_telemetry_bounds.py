"""Direct integrations must honor the same SQLite limits as HTTP forms."""
from typing import Any
from unittest.mock import Mock

import pytest

from wp_fleet_ops import checks
from wp_fleet_ops.fleet import FleetSite, calculate_health_score, generate_alerts
from wp_fleet_ops.storage import FleetOpsStore


SQLITE_MAX = (1 << 63) - 1
CARE_METRICS: dict[str, Any] = {
    "http_status": 200,
    "latency_ms": 250,
    "ssl_days_remaining": 60,
    "wordpress_version": "unknown",
    "update_count": 0,
    "backup_age_hours": 24,
}
FLEET_METRICS: dict[str, Any] = {
    "uptime_ok": True,
    "ssl_days": 60,
    "wp_updates": 0,
    "backup_age_hours": 24,
    "response_ms": 250,
    "security_header_count": 3,
}
CARE_FIELDS = ("latency_ms", "ssl_days_remaining", "update_count", "backup_age_hours")
FLEET_FIELDS = ("ssl_days", "wp_updates", "backup_age_hours", "response_ms")


@pytest.mark.parametrize("field", CARE_FIELDS)
@pytest.mark.parametrize("value", [SQLITE_MAX + 1, 10**100])
def test_direct_care_check_rejects_unpersistable_telemetry(field, value):
    with pytest.raises(ValueError, match=rf"^{field} must be at most {SQLITE_MAX}\.$"):
        checks.evaluate_site(
            "Direct Care", "https://direct-care.example",
            **(CARE_METRICS | {field: value}),
        )


@pytest.mark.parametrize("field", FLEET_FIELDS)
@pytest.mark.parametrize("value", [SQLITE_MAX + 1, 10**100])
def test_direct_fleet_site_rejects_unpersistable_telemetry(field, value):
    with pytest.raises(ValueError, match=rf"^{field} must be at most {SQLITE_MAX}\.$"):
        FleetSite(
            "Direct Fleet", "https://direct-fleet.example",
            **(FLEET_METRICS | {field: value}),
        )


@pytest.mark.parametrize("field", ["update_count", "backup_age_hours"])
def test_direct_fetch_rejects_oversized_metadata_before_network_io(monkeypatch, field):
    network = Mock(side_effect=AssertionError("Invalid input must not reach the network"))
    monkeypatch.setattr(checks.urllib.request, "urlopen", network)
    monkeypatch.setattr(checks, "ssl_days_remaining", network)

    metadata: dict[str, Any] = {field: SQLITE_MAX + 1}
    with pytest.raises(ValueError, match=rf"^{field} must be at most {SQLITE_MAX}\.$"):
        checks.fetch_basic_site_check(
            "Direct Fetch", "https://direct-fetch.example", **metadata,
        )
    network.assert_not_called()


def test_maximum_sqlite_telemetry_round_trips_in_a_combined_observation(tmp_path):
    store = FleetOpsStore(tmp_path / "bounded.sqlite3")
    care_metrics: dict[str, Any] = CARE_METRICS | dict.fromkeys(CARE_FIELDS, SQLITE_MAX)
    fleet_metrics: dict[str, Any] = FLEET_METRICS | dict.fromkeys(FLEET_FIELDS, SQLITE_MAX)
    care = checks.evaluate_site(
        "Maximum", "https://maximum.example",
        **care_metrics,
    )
    fleet = FleetSite(
        care.name, care.url,
        **fleet_metrics,
    )
    store.save_observation(
        care.name, care.url, "Limits", fleet,
        calculate_health_score(fleet), generate_alerts(fleet), care,
    )

    saved_care = store.latest_care_checks()[0]
    saved_fleet = store.latest_dashboard()[0]
    assert {field: saved_care[field] for field in CARE_FIELDS} == dict.fromkeys(CARE_FIELDS, SQLITE_MAX)
    assert {field: saved_fleet[field] for field in FLEET_FIELDS} == dict.fromkeys(FLEET_FIELDS, SQLITE_MAX)
    assert store.health_counts() == {"sites": 1, "care_checks": 1, "fleet_snapshots": 1}
