import pytest

from wp_fleet_ops.checks import evaluate_site, normalize_site_url, summarize_care_report
from wp_fleet_ops.fleet import FleetSite, calculate_health_score, generate_alerts, generate_maintenance_report
from wp_fleet_ops.storage import FleetOpsStore


def test_care_score_and_report_are_client_friendly():
    good = evaluate_site("Church", "church.example", 200, 200, 90, "6.6", 0, 12, {"strict-transport-security": "max-age=1", "x-frame-options": "SAMEORIGIN"})
    bad = evaluate_site("Client", "https://client.example", 500, 1800, 5, "6.2", 6, 120, {})

    assert good.status == "green"
    assert bad.status == "red"
    report = summarize_care_report([good, bad])
    assert "Monthly WordPress Care Report" in report
    assert "Needs attention" in report


def test_fleet_alerts_and_report_group_operational_risk():
    site = FleetSite("Client", "https://client.example", False, 5, 6, 100, 2600, 0)

    assert calculate_health_score(site) < 65
    alerts = generate_alerts(site)
    assert any(a.severity == "critical" and "down" in a.message.lower() for a in alerts)
    assert "WP FleetOps Maintenance Report" in generate_maintenance_report([site])


def test_store_combines_sites_care_checks_and_snapshots(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")
    site_id = store.upsert_site("Church", "HTTPS://Church.Example/#overview", "Church Client")
    duplicate_id = store.upsert_site("Church", "https://church.example", "Church Client")
    check = evaluate_site("Church", "https://church.example", 200, 180, 90, "6.6.1", 1, 20, {})
    fleet_site = FleetSite("Church", "https://church.example", True, 90, 1, 20, 180, 3)

    assert duplicate_id == site_id
    assert store.list_sites()[0]["url"] == "https://church.example"
    assert store.save_care_check(site_id, check) > 0
    assert store.save_snapshot(site_id, fleet_site, calculate_health_score(fleet_site), generate_alerts(fleet_site)) > 0
    assert store.latest_care_checks()[0]["client"] == "Church Client"
    assert store.latest_dashboard()[0]["name"] == "Church"


def test_normalize_site_url_deduplicates_bare_domains():
    assert normalize_site_url("Example.COM/") == "https://example.com"


def test_normalize_site_url_strips_client_only_fragments():
    assert normalize_site_url("HTTPS://Example.COM/#dashboard") == "https://example.com"
    assert normalize_site_url("https://example.com/status?view=full#summary") == "https://example.com/status?view=full"


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("https://Example.COM:443/", "https://example.com"),
        ("http://Example.COM:80/status", "http://example.com/status"),
        ("https://[2001:DB8::1]:443/", "https://[2001:db8::1]"),
        ("https://Example.COM:8443/", "https://example.com:8443"),
    ],
)
def test_normalize_site_url_strips_only_default_ports(url, normalized):
    assert normalize_site_url(url) == normalized


def test_store_deduplicates_default_port_urls(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    first_id = store.upsert_site("Default HTTPS", "https://example.com:443")
    duplicate_id = store.upsert_site("Default HTTPS", "https://example.com")

    assert duplicate_id == first_id
    assert len(store.list_sites()) == 1


def test_normalize_site_url_deduplicates_fully_qualified_hostnames():
    assert normalize_site_url("HTTPS://Example.COM./") == "https://example.com"
    assert normalize_site_url("https://Example.COM.:8443/status") == "https://example.com:8443/status"


def test_store_deduplicates_trailing_dot_hostnames(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    first_id = store.upsert_site("FQDN Site", "https://example.com.")
    duplicate_id = store.upsert_site("FQDN Site", "https://example.com")

    assert duplicate_id == first_id
    assert len(store.list_sites()) == 1


def test_store_deduplicates_unicode_and_punycode_hostnames(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    first_id = store.upsert_site("International Site", "https://BÜCHER.example/status")
    duplicate_id = store.upsert_site("International Site", "https://xn--bcher-kva.example/status")

    assert duplicate_id == first_id
    assert store.list_sites()[0]["url"] == "https://xn--bcher-kva.example/status"
    assert len(store.list_sites()) == 1


def test_store_normalizes_site_labels_and_rejects_blank_names(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    store.upsert_site("  Church Site  ", "church.example", "  Church Client  ")

    site = store.list_sites()[0]
    assert site["name"] == "Church Site"
    assert site["client"] == "Church Client"
    with pytest.raises(ValueError, match="Site name must not be blank"):
        store.upsert_site(" \t ", "blank.example", "Client")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://",
        "https://admin@example.com",
        "https://admin:secret@example.com",
        "https://example.com/path with space",
        "https://example.com/search?q=hello world",
    ],
)
def test_normalize_site_url_rejects_unsafe_or_hostless_urls(url):
    with pytest.raises(ValueError, match="valid HTTP or HTTPS URL"):
        normalize_site_url(url)
