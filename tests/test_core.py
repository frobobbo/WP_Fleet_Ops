from dataclasses import replace
from email.message import Message
from pathlib import Path
import sqlite3
from urllib.error import HTTPError

import pytest

from wp_fleet_ops.checks import evaluate_site, fetch_basic_site_check, normalize_site_url, summarize_care_report
from wp_fleet_ops.fleet import FleetSite, calculate_health_score, generate_alerts, generate_maintenance_report
from wp_fleet_ops.storage import FleetOpsStore


def test_container_healthcheck_requires_database_readiness():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
    healthcheck = next(
        line for line in dockerfile.splitlines() if line.startswith("HEALTHCHECK ")
    )

    assert "/ready" in healthcheck
    assert "/health" not in healthcheck


def test_helm_connection_test_checks_required_app_surfaces():
    helm_test = (
        Path(__file__).parents[1]
        / "charts"
        / "wp-fleet-ops"
        / "templates"
        / "tests"
        / "test-connection.yaml"
    ).read_text()

    assert "command: ['/bin/sh', '-ec']" in helm_test
    assert '"status":"ready"' in helm_test
    assert 'grep -Fq "\\\"revision\\\":\\\"${expected_revision}\\\""' in helm_test
    assert "grep -Fq '\"revision\":\"${expected_revision}\"'" not in helm_test
    assert '"${base_url}/ready"' in helm_test
    assert '"${base_url}/"' in helm_test
    assert '"${base_url}/report"' in helm_test
    assert "WP FleetOps Maintenance Report" in helm_test
    assert '"helm.sh/hook-delete-policy": before-hook-creation' in helm_test
    assert "hook-succeeded" not in helm_test
    assert "activeDeadlineSeconds: 120" in helm_test
    assert "/health" not in helm_test


def test_helm_connection_test_uses_restricted_security_context():
    helm_test = (
        Path(__file__).parents[1]
        / "charts"
        / "wp-fleet-ops"
        / "templates"
        / "tests"
        / "test-connection.yaml"
    ).read_text()

    assert "automountServiceAccountToken: false" in helm_test
    assert "runAsNonRoot: true" in helm_test
    assert "runAsUser: 65534" in helm_test
    assert "runAsGroup: 65534" in helm_test
    assert "seccompProfile:\n      type: RuntimeDefault" in helm_test
    assert "allowPrivilegeEscalation: false" in helm_test
    assert "readOnlyRootFilesystem: true" in helm_test
    assert "capabilities:\n          drop: [\"ALL\"]" in helm_test


def test_helm_workload_disables_unused_service_account_token_by_default():
    chart = Path(__file__).parents[1] / "charts" / "wp-fleet-ops"
    values = (chart / "values.yaml").read_text()
    deployment = (chart / "templates" / "deployment.yaml").read_text()

    assert "serviceAccount:\n  create: true\n  automount: false" in values
    assert "automountServiceAccountToken: {{ .Values.serviceAccount.automount }}" in deployment


def test_helm_workload_disables_unused_service_link_environment_variables():
    """The app uses DNS and must not inherit ambient Service metadata."""
    chart = Path(__file__).parents[1] / "charts" / "wp-fleet-ops"
    values = (chart / "values.yaml").read_text()
    deployment = (chart / "templates" / "deployment.yaml").read_text()

    assert "enableServiceLinks: false" in values
    assert "enableServiceLinks: {{ .Values.enableServiceLinks }}" in deployment


def test_helm_workload_avoids_recursive_pvc_ownership_changes_on_every_start():
    """Retained SQLite data should not be recursively relabeled every rollout."""
    values = (
        Path(__file__).parents[1]
        / "charts"
        / "wp-fleet-ops"
        / "values.yaml"
    ).read_text()

    assert "podSecurityContext:\n  fsGroup: 1000" in values
    assert "  fsGroupChangePolicy: OnRootMismatch\n" in values


def test_helm_workload_exposes_configured_git_revision():
    chart = Path(__file__).parents[1] / "charts" / "wp-fleet-ops"
    values = (chart / "values.yaml").read_text()
    deployment = (chart / "templates" / "deployment.yaml").read_text()

    assert "config:\n  dataDir: /data" in values
    assert "  revision: unknown" in values
    assert "- name: WP_FLEET_OPS_REVISION" in deployment
    assert 'value: {{ .Values.config.revision | default "unknown" | quote }}' in deployment


def test_helm_persistent_data_is_retained_by_default():
    """Removing a Helm release must not silently delete FleetOps history."""
    chart = Path(__file__).parents[1] / "charts" / "wp-fleet-ops"
    values = (chart / "values.yaml").read_text()
    pvc = (chart / "templates" / "pvc.yaml").read_text()

    assert "persistence:\n  enabled: true" in values
    assert "\n  keep: true" in values
    assert "{{- if .Values.persistence.keep }}" in pvc
    assert '"helm.sh/resource-policy": keep' in pvc


def test_helm_default_latest_image_is_always_pulled():
    """A rollout must not silently reuse a node-cached mutable latest image."""
    values = (
        Path(__file__).parents[1]
        / "charts"
        / "wp-fleet-ops"
        / "values.yaml"
    ).read_text()

    assert "  repository: ghcr.io/frobobbo/wp_fleet_ops\n" in values
    assert "  pullPolicy: Always\n  tag: \"latest\"" in values


def test_helm_deployment_bounds_retained_revisions():
    """Frequent maintenance rollouts should retain bounded rollback history."""
    chart = Path(__file__).parents[1] / "charts" / "wp-fleet-ops"
    values = (chart / "values.yaml").read_text()
    deployment = (chart / "templates" / "deployment.yaml").read_text()

    assert "revisionHistoryLimit: 3" in values
    assert "revisionHistoryLimit: {{ .Values.revisionHistoryLimit }}" in deployment


def test_helm_workload_has_bounded_database_ready_startup_probe():
    """Slow source-bundle setup must not be killed by liveness checks."""
    chart = Path(__file__).parents[1] / "charts" / "wp-fleet-ops"
    values = (chart / "values.yaml").read_text()
    deployment = (chart / "templates" / "deployment.yaml").read_text()

    assert (
        "startupProbe:\n"
        "  httpGet:\n"
        "    path: /ready\n"
        "    port: http\n"
        "  periodSeconds: 5\n"
        "  timeoutSeconds: 2\n"
        "  failureThreshold: 36"
    ) in values
    assert "startupProbe:\n            {{- toYaml .Values.startupProbe | nindent 12 }}" in deployment


def test_helm_readiness_probe_outlives_bounded_database_lock_wait():
    """The probe must let /ready return its bounded SQLite contention result."""
    values = (
        Path(__file__).parents[1]
        / "charts"
        / "wp-fleet-ops"
        / "values.yaml"
    ).read_text()

    readiness_values = values.split("\nreadinessProbe:\n", 1)[1].split(
        "\nnodeSelector:", 1
    )[0]

    assert "    path: /ready" in readiness_values
    assert "  timeoutSeconds: 2" in readiness_values


def test_helm_rollout_requires_sustained_readiness_before_availability():
    """A single successful probe must not prematurely complete the rollout."""
    chart = Path(__file__).parents[1] / "charts" / "wp-fleet-ops"
    values = (chart / "values.yaml").read_text()
    deployment = (chart / "templates" / "deployment.yaml").read_text()

    assert "minReadySeconds: 5" in values
    assert "minReadySeconds: {{ .Values.minReadySeconds }}" in deployment


def test_helm_source_bundle_init_container_uses_restricted_security_context():
    deployment = (
        Path(__file__).parents[1]
        / "charts"
        / "wp-fleet-ops"
        / "templates"
        / "deployment.yaml"
    ).read_text()

    restricted_context = "securityContext:\n            {{- toYaml .Values.securityContext | nindent 12 }}"
    assert deployment.count(restricted_context) == 2
    assert (
        "- name: unpack-source\n"
        "          securityContext:\n"
        "            {{- toYaml .Values.securityContext | nindent 12 }}"
    ) in deployment


def test_helm_workload_pins_nonroot_primary_group():
    """Stock fallback images must not inherit GID 0 from their image default."""
    values = (
        Path(__file__).parents[1]
        / "charts"
        / "wp-fleet-ops"
        / "values.yaml"
    ).read_text()

    security_context = values.split("\nsecurityContext:\n", 1)[1].split("\nservice:\n", 1)[0]
    assert "  runAsUser: 100\n" in security_context
    assert "  runAsGroup: 1000\n" in security_context


def test_helm_source_bundle_projects_configured_archive_key():
    deployment = (
        Path(__file__).parents[1]
        / "charts"
        / "wp-fleet-ops"
        / "templates"
        / "deployment.yaml"
    ).read_text()

    assert "key: {{ .Values.sourceBundle.fileName | quote }}" in deployment
    assert "path: app.tar.gz" in deployment


def test_helm_workload_uses_read_only_root_with_bounded_runtime_tmp():
    chart = Path(__file__).parents[1] / "charts" / "wp-fleet-ops"
    values = (chart / "values.yaml").read_text()
    deployment = (chart / "templates" / "deployment.yaml").read_text()

    assert "seccompProfile:\n    type: RuntimeDefault" in values
    assert "readOnlyRootFilesystem: true" in values
    assert deployment.count("mountPath: /tmp") == 2
    assert deployment.count("- name: runtime-tmp") == 3
    assert "emptyDir:\n            sizeLimit: 64Mi" in deployment


def test_helm_source_bundle_bounds_dependency_install_storage():
    """Fallback installs must not consume unbounded node ephemeral storage."""
    chart = Path(__file__).parents[1] / "charts" / "wp-fleet-ops"
    values = (chart / "values.yaml").read_text()
    deployment = (chart / "templates" / "deployment.yaml").read_text()

    assert "  workSizeLimit: 512Mi\n" in values
    assert (
        "- name: source-work\n"
        "          emptyDir:\n"
        "            sizeLimit: {{ .Values.sourceBundle.workSizeLimit }}"
    ) in deployment


def test_care_score_and_report_are_client_friendly():
    good = evaluate_site("Church", "church.example", 200, 200, 90, "6.6", 0, 12, {"strict-transport-security": "max-age=1", "x-frame-options": "SAMEORIGIN"})
    bad = evaluate_site("Client", "https://client.example", 500, 1800, 5, "6.2", 6, 120, {})

    assert good.status == "green"
    assert bad.status == "red"
    report = summarize_care_report([good, bad])
    assert "Monthly WordPress Care Report" in report
    assert "Needs attention" in report


def test_care_evaluation_rejects_oversized_wordpress_versions():
    with pytest.raises(
        ValueError,
        match="WordPress version must be 100 characters or fewer",
    ):
        evaluate_site(
            "Bounded Version",
            "https://bounded-version.example",
            200,
            250,
            60,
            "v" * 101,
            0,
            24,
            {},
        )


def test_care_evaluation_normalizes_wordpress_version_values():
    normalized = evaluate_site(
        "Normalized Version",
        "https://normalized-version.example",
        200,
        250,
        60,
        "  6.6.1  ",
        0,
        24,
        {},
    )
    unknown = evaluate_site(
        "Unknown Version",
        "https://unknown-version.example",
        200,
        250,
        60,
        "  ",
        0,
        24,
        {},
    )

    assert normalized.wordpress_version == "6.6.1"
    assert unknown.wordpress_version == "unknown"


@pytest.mark.parametrize("wordpress_version", ["6.6\nforged", "6.6\u202eforged"])
def test_care_evaluation_rejects_nonprinting_wordpress_versions(wordpress_version):
    with pytest.raises(
        ValueError,
        match="WordPress version must contain only printable characters",
    ):
        evaluate_site(
            "Printable Version",
            "https://printable-version.example",
            200,
            250,
            60,
            wordpress_version,
            0,
            24,
            {},
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("latency_ms", -1),
        ("ssl_days_remaining", -1),
        ("update_count", -1),
        ("backup_age_hours", -1),
    ],
)
def test_care_evaluation_rejects_negative_telemetry(field, value):
    metrics = {
        "http_status": 200,
        "latency_ms": 250,
        "ssl_days_remaining": 60,
        "update_count": 0,
        "backup_age_hours": 24,
    }
    metrics[field] = value

    with pytest.raises(ValueError, match=f"{field} must not be negative"):
        evaluate_site(
            "Telemetry Integrity",
            "https://telemetry-integrity.example",
            metrics["http_status"],
            metrics["latency_ms"],
            metrics["ssl_days_remaining"],
            "6.6",
            metrics["update_count"],
            metrics["backup_age_hours"],
            {},
        )


@pytest.mark.parametrize("http_status", [-1, 99, 600])
def test_care_evaluation_rejects_invalid_http_statuses(http_status):
    with pytest.raises(ValueError, match="http_status must be 0 or between 100 and 599"):
        evaluate_site(
            "HTTP Integrity",
            "https://http-integrity.example",
            http_status,
            250,
            60,
            "6.6",
            0,
            24,
            {},
        )


def test_care_evaluation_retains_zero_http_failure_sentinel():
    check = evaluate_site(
        "Unavailable Site",
        "https://unavailable-site.example",
        0,
        250,
        0,
        "unknown",
        0,
        24,
        {},
    )

    assert check.http_status == 0
    assert check.status == "red"


def test_dashboard_bounds_persisted_text_inputs():
    dashboard = (Path(__file__).parents[1] / "templates" / "index.html").read_text()

    assert dashboard.count('name="name" maxlength="200"') == 2
    assert dashboard.count('name="url" maxlength="2048"') == 2
    assert dashboard.count('name="client" maxlength="200"') == 2
    assert 'name="wordpress_version" maxlength="100"' in dashboard


def test_fleet_alerts_and_report_group_operational_risk():
    site = FleetSite("Client", "https://client.example", False, 5, 6, 100, 2600, 0)

    assert calculate_health_score(site) < 65
    alerts = generate_alerts(site)
    assert any(a.severity == "critical" and "down" in a.message.lower() for a in alerts)
    assert "WP FleetOps Maintenance Report" in generate_maintenance_report([site])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ssl_days", -1),
        ("wp_updates", -1),
        ("backup_age_hours", -1),
        ("response_ms", -1),
        ("security_header_count", -1),
        ("security_header_count", 4),
    ],
)
def test_fleet_site_rejects_invalid_telemetry(field, value):
    metrics = {
        "ssl_days": 60,
        "wp_updates": 0,
        "backup_age_hours": 24,
        "response_ms": 250,
        "security_header_count": 3,
    }
    metrics[field] = value

    with pytest.raises(ValueError, match=field):
        FleetSite(
            "Fleet Integrity",
            "https://fleet-integrity.example",
            True,
            metrics["ssl_days"],
            metrics["wp_updates"],
            metrics["backup_age_hours"],
            metrics["response_ms"],
            metrics["security_header_count"],
        )


def test_fleet_alerts_treat_seven_day_certificate_as_critical():
    site = FleetSite("Renewal", "https://renewal.example", True, 7, 0, 24, 250, 3)

    certificate_alert = next(alert for alert in generate_alerts(site) if "SSL expires" in alert.message)

    assert certificate_alert.severity == "critical"


def test_fleet_alerts_surface_thirty_day_certificate_renewals():
    site = FleetSite("Renewal", "https://renewal.example", True, 30, 0, 24, 250, 3)

    certificate_alert = next(alert for alert in generate_alerts(site) if "SSL expires" in alert.message)

    assert calculate_health_score(site) == 90
    assert certificate_alert.severity == "warning"
    assert certificate_alert.message == "SSL expires in 30 day(s)."


def test_fleet_alerts_surface_aging_backups_before_they_become_critical():
    site = FleetSite("Backup Watch", "https://backup-watch.example", True, 60, 0, 48, 250, 3)

    backup_alert = next(alert for alert in generate_alerts(site) if "backup" in alert.message.lower())

    assert backup_alert.severity == "warning"
    assert backup_alert.message == "Latest backup is 48 hours old."


def test_fleet_alerts_escalate_critical_update_backlogs():
    critical_site = FleetSite("Critical Updates", "https://critical-updates.example", True, 60, 5, 24, 250, 3)
    warning_site = FleetSite("Routine Updates", "https://routine-updates.example", True, 60, 4, 24, 250, 3)

    critical_alert = next(alert for alert in generate_alerts(critical_site) if "updates pending" in alert.message)
    warning_alert = next(alert for alert in generate_alerts(warning_site) if "updates pending" in alert.message)

    assert critical_alert.severity == "critical"
    assert warning_alert.severity == "warning"


def test_fleet_performance_threshold_matches_care_checks():
    care_check = evaluate_site(
        "Performance Watch",
        "https://performance-watch.example",
        200,
        1201,
        60,
        "6.6",
        0,
        24,
        {"strict-transport-security": "max-age=1", "x-frame-options": "SAMEORIGIN"},
    )
    fleet_site = FleetSite("Performance Watch", care_check.url, True, 60, 0, 24, 1201, 3)

    performance_alert = next(alert for alert in generate_alerts(fleet_site) if "response time" in alert.message.lower())

    assert any("Improve performance" in action for action in care_check.actions)
    assert calculate_health_score(fleet_site) == care_check.score == 90
    assert performance_alert.severity == "warning"
    assert performance_alert.message == "Homepage response time is 1201 ms."


@pytest.mark.parametrize(
    ("security_header_count", "security_headers", "expected_score"),
    [
        (0, {}, 92),
        (1, {"strict-transport-security": "max-age=1"}, 96),
        (2, {"strict-transport-security": "max-age=1", "x-frame-options": "SAMEORIGIN"}, 100),
    ],
)
def test_fleet_security_score_matches_care_checks(security_header_count, security_headers, expected_score):
    care_check = evaluate_site(
        "Security Coverage",
        "https://security-coverage.example",
        200,
        250,
        60,
        "6.6",
        0,
        24,
        security_headers,
    )
    fleet_site = FleetSite("Security Coverage", care_check.url, True, 60, 0, 24, 250, security_header_count)

    assert calculate_health_score(fleet_site) == care_check.score == expected_score


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


def test_store_rolls_back_entire_paired_observation_when_care_insert_fails(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")
    fleet_site = FleetSite(
        "Atomic Site",
        "https://atomic.example",
        True,
        90,
        0,
        12,
        180,
        3,
    )
    check = evaluate_site(
        "Atomic Site",
        "https://atomic.example",
        200,
        180,
        90,
        "6.6.1",
        0,
        12,
        {
            "strict-transport-security": "max-age=31536000",
            "x-frame-options": "SAMEORIGIN",
        },
    )
    oversized_check = replace(check, latency_ms=1 << 63)

    with pytest.raises(OverflowError, match="too large to convert to SQLite INTEGER"):
        store.save_observation(
            "Atomic Site",
            "https://atomic.example",
            "Atomic Client",
            fleet_site,
            calculate_health_score(fleet_site),
            generate_alerts(fleet_site),
            oversized_check,
        )

    assert store.health_counts() == {
        "sites": 0,
        "care_checks": 0,
        "fleet_snapshots": 0,
    }


def test_store_rejects_orphan_check_and_snapshot_rows(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")
    check = evaluate_site("Missing", "missing.example", 200, 180, 90, "6.6.1", 0, 20, {})
    fleet_site = FleetSite("Missing", "https://missing.example", True, 90, 0, 20, 180, 3)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        store.save_care_check(999, check)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        store.save_snapshot(999, fleet_site, calculate_health_score(fleet_site), generate_alerts(fleet_site))


def test_store_configures_connections_for_concurrent_requests(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    with store._connect() as con:
        journal_mode = con.execute("pragma journal_mode").fetchone()[0]
        busy_timeout = con.execute("pragma busy_timeout").fetchone()[0]
        foreign_keys = con.execute("pragma foreign_keys").fetchone()[0]

    assert journal_mode == "wal"
    assert busy_timeout == 30_000
    assert foreign_keys == 1


def test_store_health_counts_rejects_read_only_database(tmp_path, monkeypatch):
    db_path = tmp_path / "fleetops.sqlite3"
    store = FleetOpsStore(db_path)

    def read_only_connection():
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    monkeypatch.setattr(store, "_connect", read_only_connection)

    with pytest.raises(sqlite3.OperationalError, match="readonly database"):
        store.health_counts()


def test_store_health_counts_bounds_database_lock_wait(tmp_path, monkeypatch):
    """Readiness must fail before Kubernetes times out its two-second probe."""
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")
    connection = store._connect()
    assert connection.execute("pragma busy_timeout").fetchone()[0] == 30_000
    monkeypatch.setattr(store, "_connect", lambda: connection)

    store.health_counts()

    assert connection.execute("pragma busy_timeout").fetchone()[0] == 1_000
    connection.close()


def test_store_indexes_per_site_history_queries(tmp_path):
    db_path = tmp_path / "fleetops.sqlite3"
    FleetOpsStore(db_path)

    with sqlite3.connect(db_path) as con:
        care_indexes = {row[1] for row in con.execute("pragma index_list(care_checks)")}
        snapshot_indexes = {row[1] for row in con.execute("pragma index_list(snapshots)")}

    assert "idx_care_checks_site_id_id" in care_indexes
    assert "idx_snapshots_site_id_id" in snapshot_indexes


def test_store_indexes_client_history_filters(tmp_path):
    db_path = tmp_path / "fleetops.sqlite3"
    FleetOpsStore(db_path)

    with sqlite3.connect(db_path) as con:
        site_indexes = {row[1] for row in con.execute("pragma index_list(sites)")}
        indexed_columns = [
            row[2]
            for row in con.execute("pragma index_info(idx_sites_client_id)")
        ]

    assert "idx_sites_client_id" in site_indexes
    assert indexed_columns == ["client", "id"]


def test_normalize_site_url_deduplicates_bare_domains():
    assert normalize_site_url("Example.COM/") == "https://example.com"


def test_normalize_site_url_accepts_scheme_less_ports_with_query_or_fragment():
    assert normalize_site_url("Example.COM:8443?view=full") == (
        "https://example.com:8443?view=full"
    )
    assert normalize_site_url("Example.COM:8443#status") == "https://example.com:8443"


def test_normalize_site_url_strips_client_only_fragments():
    assert normalize_site_url("HTTPS://Example.COM/#dashboard") == "https://example.com"
    assert normalize_site_url("https://example.com/status?view=full#summary") == "https://example.com/status?view=full"


def test_normalize_site_url_deduplicates_root_query_paths():
    assert normalize_site_url("https://Example.COM/?view=full") == "https://example.com?view=full"


def test_store_deduplicates_root_query_paths(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    first_id = store.upsert_site("Query Site", "https://example.com/?view=full")
    duplicate_id = store.upsert_site("Query Site", "https://example.com?view=full")

    assert duplicate_id == first_id
    assert len(store.list_sites()) == 1


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


def test_store_deduplicates_equivalent_ipv6_hostnames(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    first_id = store.upsert_site(
        "IPv6 Site",
        "https://[2001:0DB8:0000:0000:0000:0000:0000:0001]:443/",
    )
    duplicate_id = store.upsert_site("IPv6 Site", "https://[2001:db8::1]")

    assert duplicate_id == first_id
    assert store.list_sites()[0]["url"] == "https://[2001:db8::1]"
    assert len(store.list_sites()) == 1


def test_normalize_site_url_accepts_canonical_ipv4_hosts():
    assert normalize_site_url("HTTP://127.0.0.1:80/") == "http://127.0.0.1"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.1",
        "http://2130706433",
        "http://0177.0.0.1",
        "http://0x7f.0.0.1",
        "https://192.168.015.005",
    ],
)
def test_normalize_site_url_rejects_ambiguous_ipv4_hosts(url):
    with pytest.raises(ValueError, match="valid HTTP or HTTPS URL"):
        normalize_site_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example..com",
        "https://-example.com",
        "https://example-.com",
        "https://example_.com",
        f"https://{'a' * 64}.example",
    ],
)
def test_normalize_site_url_rejects_malformed_dns_hostnames(url):
    with pytest.raises(ValueError, match="valid HTTP or HTTPS URL"):
        normalize_site_url(url)


def test_normalize_site_url_deduplicates_fully_qualified_hostnames():
    assert normalize_site_url("HTTPS://Example.COM./") == "https://example.com"
    assert normalize_site_url("https://Example.COM.:8443/status") == "https://example.com:8443/status"


def test_normalize_site_url_deduplicates_unicode_fully_qualified_hostnames():
    assert normalize_site_url("HTTPS://BÜCHER.example。/") == (
        "https://xn--bcher-kva.example"
    )


def test_normalize_site_url_decodes_encoded_hostname_dots_only():
    assert normalize_site_url("HTTPS://Example%2eCOM/path%2epart") == (
        "https://example.com/path%2Epart"
    )


def test_store_deduplicates_percent_encoded_hostname_dots(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    first_id = store.upsert_site("Encoded Host", "https://example%2Ecom/status")
    duplicate_id = store.upsert_site("Encoded Host", "https://example.com/status")

    assert duplicate_id == first_id
    assert store.list_sites()[0]["url"] == "https://example.com/status"
    assert len(store.list_sites()) == 1


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
    ("name", "client", "message"),
    [
        ("Church Site\n# Forged report heading", "Client", "Site name must contain only printable characters"),
        ("Church Site", "Client\x00Hidden", "Client name must contain only printable characters"),
        ("Church Site\u202e", "Client", "Site name must contain only printable characters"),
    ],
)
def test_store_rejects_nonprinting_site_labels(tmp_path, name, client, message):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    with pytest.raises(ValueError, match=message):
        store.upsert_site(name, "https://printable-labels.example", client)

    assert store.list_sites() == []


@pytest.mark.parametrize(
    ("name", "url", "client", "message"),
    [
        ("n" * 201, "https://bounded.example", "Client", "Site name must be 200 characters or fewer"),
        (
            "Bounded Site",
            "https://bounded.example/" + "p" * 2048,
            "Client",
            "Site URL must be 2048 characters or fewer",
        ),
        ("Bounded Site", "https://bounded.example", "c" * 201, "Client name must be 200 characters or fewer"),
    ],
)
def test_store_rejects_oversized_site_identifiers(tmp_path, name, url, client, message):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    with pytest.raises(ValueError, match=message):
        store.upsert_site(name, url, client)

    assert store.list_sites() == []


def test_normalize_site_url_rejects_canonical_url_over_length_limit():
    bare_url = "example.com/" + "p" * (2048 - len("example.com/"))
    assert len(bare_url) == 2048

    with pytest.raises(ValueError, match="Site URL must be 2048 characters or fewer"):
        normalize_site_url(bare_url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://",
        "https://admin@example.com",
        "https://admin:secret@example.com",
        r"https://example.com\misleading.example/status",
        r"https://example.com/status\archive",
        "https://example.com/path with space",
        "https://example.com/search?q=hello world",
        "https://example.com/status\x00hidden",
        "https://example.com/status\x7fhidden",
        "https://example.com/status\u200bhidden",
        "https://example.com/status\u202eevil.test",
        "https://example.com/status%00hidden",
        "https://example.com/%0d%0aHost:evil.test",
        "https://example.com/status%",
        "https://example.com/status%zz",
        "https://example.com:",
        "https://example.com:/status",
        "https://[2001:db8::1",
        "https://2001:db8::1]",
    ],
)
def test_normalize_site_url_rejects_unsafe_or_hostless_urls(url):
    with pytest.raises(ValueError, match="valid HTTP or HTTPS URL"):
        normalize_site_url(url)


def test_normalize_site_url_preserves_valid_percent_encoded_path_and_query_values():
    assert normalize_site_url("https://example.com/a%20b?q=x%2Fy") == (
        "https://example.com/a%20b?q=x%2Fy"
    )


def test_normalize_site_url_canonicalizes_percent_escape_hex_case():
    assert normalize_site_url("https://example.com/a%2fb?q=x%3ay") == (
        "https://example.com/a%2Fb?q=x%3Ay"
    )


def test_normalize_site_url_decodes_percent_encoded_unreserved_characters():
    assert normalize_site_url(
        "https://example.com/%7euser/%41pi?view=%66ull&tag=%2D"
    ) == "https://example.com/~user/Api?view=full&tag=-"


def test_normalize_site_url_decodes_encoded_query_dots_but_preserves_path_dots():
    assert normalize_site_url(
        "https://example.com/path%2epart?release=%2e&range=%2E%2E"
    ) == "https://example.com/path%2Epart?release=.&range=.."


def test_store_deduplicates_percent_encoded_unreserved_characters(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    first_id = store.upsert_site("Encoded Site", "https://example.com/%7Euser?view=%66ull")
    duplicate_id = store.upsert_site("Encoded Site", "https://example.com/~user?view=full")

    assert duplicate_id == first_id
    assert store.list_sites()[0]["url"] == "https://example.com/~user?view=full"
    assert len(store.list_sites()) == 1


def test_store_deduplicates_percent_encoded_query_dots(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    first_id = store.upsert_site("Encoded Query", "https://example.com/api?release=%2E")
    duplicate_id = store.upsert_site("Encoded Query", "https://example.com/api?release=.")

    assert duplicate_id == first_id
    assert store.list_sites()[0]["url"] == "https://example.com/api?release=."
    assert len(store.list_sites()) == 1


def test_normalize_site_url_encodes_unicode_path_parameters_and_query():
    assert normalize_site_url("https://example.com/café;résumé?topic=naïve") == (
        "https://example.com/caf%C3%A9;r%C3%A9sum%C3%A9?topic=na%C3%AFve"
    )


def test_fetch_basic_site_check_requests_an_ascii_uri_for_unicode_paths(monkeypatch):
    requested_urls = []

    class SuccessfulResponse:
        status = 200
        headers = Message()

        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.url

    def successful(request, timeout):
        requested_urls.append((request.full_url, timeout))
        return SuccessfulResponse(request.full_url)

    monkeypatch.setattr("wp_fleet_ops.checks.urllib.request.urlopen", successful)

    check = fetch_basic_site_check(
        "Unicode Page",
        "http://example.com/café?topic=naïve",
        timeout=7,
    )

    expected_url = "http://example.com/caf%C3%A9?topic=na%C3%AFve"
    assert requested_urls == [(expected_url, 7)]
    assert check.url == expected_url
    assert check.http_status == 200


def test_store_deduplicates_unicode_and_percent_encoded_paths(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    first_id = store.upsert_site("Unicode Site", "https://example.com/café?topic=naïve")
    duplicate_id = store.upsert_site(
        "Unicode Site",
        "https://example.com/caf%C3%A9?topic=na%C3%AFve",
    )

    assert duplicate_id == first_id
    assert store.list_sites()[0]["url"] == (
        "https://example.com/caf%C3%A9?topic=na%C3%AFve"
    )
    assert len(store.list_sites()) == 1


def test_store_deduplicates_percent_escape_hex_case(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    first_id = store.upsert_site("Encoded Site", "https://example.com/a%2fb?q=x%3ay")
    duplicate_id = store.upsert_site("Encoded Site", "https://example.com/a%2Fb?q=x%3Ay")

    assert duplicate_id == first_id
    assert store.list_sites()[0]["url"] == "https://example.com/a%2Fb?q=x%3Ay"
    assert len(store.list_sites()) == 1


def test_normalize_site_url_preserves_path_parameters():
    assert normalize_site_url("https://example.com/wp-json;version=2?context=view") == (
        "https://example.com/wp-json;version=2?context=view"
    )


def test_normalize_site_url_preserves_empty_path_parameter_delimiter():
    assert normalize_site_url("https://example.com/wp-json;?context=view") == (
        "https://example.com/wp-json;?context=view"
    )


def test_normalize_site_url_preserves_empty_query_delimiter():
    assert normalize_site_url("https://example.com/status?") == (
        "https://example.com/status?"
    )
    assert normalize_site_url("https://example.com/?#dashboard") == (
        "https://example.com?"
    )


def test_store_keeps_empty_query_target_distinct(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    query_target_id = store.upsert_site("Query Target", "https://example.com/status?")
    plain_target_id = store.upsert_site("Plain Target", "https://example.com/status")

    assert query_target_id != plain_target_id
    assert {site["url"] for site in store.list_sites()} == {
        "https://example.com/status?",
        "https://example.com/status",
    }


def test_store_keeps_empty_path_parameter_target_distinct(tmp_path):
    store = FleetOpsStore(tmp_path / "fleetops.sqlite3")

    parameter_target_id = store.upsert_site(
        "Parameter Target",
        "https://example.com/wp-json;",
    )
    plain_target_id = store.upsert_site("Plain Target", "https://example.com/wp-json")

    assert parameter_target_id != plain_target_id
    assert {site["url"] for site in store.list_sites()} == {
        "https://example.com/wp-json;",
        "https://example.com/wp-json",
    }


def test_fetch_basic_site_check_preserves_http_error_status_and_headers(monkeypatch):
    headers = Message()
    headers["Strict-Transport-Security"] = "max-age=31536000"

    def unavailable(*_args, **_kwargs):
        raise HTTPError("http://unavailable.example", 503, "Service Unavailable", headers, None)

    monkeypatch.setattr("wp_fleet_ops.checks.urllib.request.urlopen", unavailable)

    check = fetch_basic_site_check("Unavailable", "http://unavailable.example")

    assert check.http_status == 503
    assert check.security_headers["strict-transport-security"] == "max-age=31536000"
    assert "HTTP status is 503" in check.actions[0]


def test_fetch_basic_site_check_uses_final_https_redirect_for_certificate(monkeypatch):
    import wp_fleet_ops.checks as checks

    headers = Message()
    headers["Strict-Transport-Security"] = "max-age=31536000"

    class RedirectedResponse:
        status = 200

        def __init__(self, response_headers):
            self.headers = response_headers

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://www.redirect.example/home"

    response = RedirectedResponse(headers)
    checked_urls = []

    monkeypatch.setattr(checks.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    def certificate_days(url, timeout=10):
        checked_urls.append((url, timeout))
        return 90

    monkeypatch.setattr(checks, "ssl_days_remaining", certificate_days)

    check = fetch_basic_site_check("Redirected", "http://redirect.example", timeout=7)

    assert check.url == "http://redirect.example"
    assert check.ssl_days_remaining == 90
    assert checked_urls == [("https://www.redirect.example/home", 7)]
