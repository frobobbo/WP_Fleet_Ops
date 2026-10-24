from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from .checks import _report_checked_at, markdown_literal


@dataclass(frozen=True)
class FleetSite:
    name: str
    url: str
    uptime_ok: bool
    ssl_days: int
    wp_updates: int
    backup_age_hours: int
    response_ms: int
    security_header_count: int

    def __post_init__(self) -> None:
        for field, value in (
            ("ssl_days", self.ssl_days),
            ("wp_updates", self.wp_updates),
            ("backup_age_hours", self.backup_age_hours),
            ("response_ms", self.response_ms),
        ):
            if value < 0:
                raise ValueError(f"{field} must not be negative.")
        if not 0 <= self.security_header_count <= 3:
            raise ValueError("security_header_count must be between 0 and 3.")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Alert:
    site: str
    severity: str
    message: str


def calculate_health_score(site: FleetSite) -> int:
    score = 100
    if not site.uptime_ok:
        score -= 45
    if site.ssl_days < 14:
        score -= 25
    elif site.ssl_days <= 30:
        score -= 10
    if site.wp_updates:
        score -= min(20, site.wp_updates * 3)
    if site.backup_age_hours > 72:
        score -= 20
    elif site.backup_age_hours > 36:
        score -= 8
    # Match CarePulse scoring so one snapshot cannot be healthy in FleetOps
    # while the paired care check already asks for performance remediation.
    if site.response_ms > 1200:
        score -= 10
    if site.security_header_count == 0:
        # CarePulse deducts four points for missing HSTS and four for missing
        # clickjacking protection. Match that breakdown so the dashboard and
        # paired care report assign the same score to count-only snapshots.
        missing_security_points = 8
    elif site.security_header_count == 1:
        missing_security_points = 4
    else:
        missing_security_points = 0
    if urlparse(site.url).scheme.lower() == "http":
        # HSTS received over HTTP is ignored by browsers. Count-only snapshots
        # therefore cannot claim complete transport-security coverage for a
        # plain-HTTP endpoint, even when all three monitored fields were reported.
        missing_security_points = max(4, missing_security_points)
    score -= missing_security_points
    return max(0, min(100, score))


def generate_alerts(
    site: FleetSite,
    security_headers: Collection[str] | None = None,
) -> list[Alert]:
    """Generate fleet alerts, using header identities when they are available."""
    alerts: list[Alert] = []
    if not site.uptime_ok:
        alerts.append(Alert(site.name, "critical", f"{site.name} appears down or unreachable."))
    if site.ssl_days < 14:
        # Keep the incident feed aligned with the certificate inventory and
        # SLA APIs: seven days remaining is already inside the critical window.
        alerts.append(Alert(site.name, "critical" if site.ssl_days <= 7 else "warning", f"SSL expires in {site.ssl_days} day(s)."))
    elif site.ssl_days <= 30:
        alerts.append(Alert(site.name, "warning", f"SSL expires in {site.ssl_days} day(s)."))
    if site.wp_updates:
        # Keep alert routing aligned with the update inventory and maintenance
        # APIs, which classify five or more pending updates as critical work.
        severity = "critical" if site.wp_updates >= 5 else "warning"
        alerts.append(Alert(site.name, severity, f"{site.wp_updates} WordPress updates pending."))
    if site.backup_age_hours > 72:
        alerts.append(Alert(site.name, "critical", f"Latest backup is {site.backup_age_hours} hours old."))
    elif site.backup_age_hours > 36:
        alerts.append(Alert(site.name, "warning", f"Latest backup is {site.backup_age_hours} hours old."))
    if site.response_ms > 1200:
        alerts.append(Alert(site.name, "warning", f"Homepage response time is {site.response_ms} ms."))
    if security_headers is None and urlparse(site.url).scheme.lower() == "http":
        alerts.append(
            Alert(
                site.name,
                "info",
                "Security headers need review: HSTS is ineffective over HTTP.",
            )
        )
    elif security_headers is None and site.security_header_count < 2:
        alerts.append(Alert(site.name, "info", "Security headers need review."))
    elif security_headers is not None:
        normalized_headers = {header.lower() for header in security_headers}
        missing_controls = []
        if (
            urlparse(site.url).scheme.lower() == "http"
            or "strict-transport-security" not in normalized_headers
        ):
            # Even when the header name is present in stored evidence, browsers
            # ignore HSTS delivered over plaintext HTTP.
            missing_controls.append("HSTS")
        if not {
            "x-frame-options",
            "content-security-policy",
        } & normalized_headers:
            missing_controls.append("clickjacking protection")
        if missing_controls:
            alerts.append(
                Alert(
                    site.name,
                    "info",
                    f"Security headers need review: missing {' and '.join(missing_controls)}.",
                )
            )
    return alerts


def _report_alert_text(value: str) -> str:
    """Keep persisted alert text on one Markdown list line, without links/markup."""
    # Alert sentences use ordinary periods, hyphens, and parentheses. These
    # cannot form Markdown links once brackets are escaped, nor introduce list
    # syntax after the fixed line prefix. Preserve existing report readability.
    escaped = markdown_literal(" ".join(value.split()))
    for mark in (".", "-", "(", ")"):
        escaped = escaped.replace("\\" + mark, mark)
    return escaped


def generate_maintenance_report(
    sites: list[FleetSite],
    *,
    saved_evidence: Mapping[str, tuple[int, list[Alert]]] | None = None,
    captured_at_by_url: Mapping[str, str | None] | None = None,
    paired_care_status_by_url: Mapping[str, str] | None = None,
    tracked_site_count: int = 0,
) -> str:
    lines = ["# WP FleetOps Maintenance Report", "", f"Generated: {datetime.now(timezone.utc).isoformat()}", ""]
    # Persisted fetches retain which header is missing. Recomputing alerts from
    # FleetSite's header count discards that evidence in exported reports.
    scored = [
        (s, *saved_evidence[s.url])
        if saved_evidence is not None
        else (s, calculate_health_score(s), generate_alerts(s))
        for s in sites
    ]
    def state_for(site: FleetSite, score: int, alerts: list[Alert]) -> tuple[str, bool]:
        fleet_status = (
            "red" if score < 65 or any(a.severity == "critical" for a in alerts)
            else "yellow" if score < 85 or any(a.severity == "warning" for a in alerts)
            else "green"
        )
        care_status = (paired_care_status_by_url or {}).get(site.url, "green")
        rank = {"green": 0, "yellow": 1, "red": 2}
        status = max((fleet_status, care_status), key=rank.__getitem__)
        return status, rank[care_status] > rank[fleet_status]

    lines += [
        f"Sites monitored: {len(sites)}",
        f"Critical sites: {sum(1 for site, score, alerts in scored if state_for(site, score, alerts)[0] == 'red')}",
        "",
    ]
    if not sites:
        if tracked_site_count:
            lines += [
                "Health unverified: tracked sites have no current paired observations.",
                "Capture a fresh combined check before reporting health.",
                "",
            ]
        else:
            lines += [
                "Health unverified: no sites are registered for this report.",
                "Register a site and capture a fleet snapshot before reporting health.",
                "",
            ]
    for site, score, alerts in scored:
        status, escalated_by_care = state_for(site, score, alerts)
        state = {"red": "Needs attention", "yellow": "Watch", "green": "Healthy"}[status]
        lines += [f"## {markdown_literal(site.name)} — {state}", "", f"Score: {score}/100", f"URL: {markdown_literal(site.url)}"]
        if captured_at_by_url is not None:
            captured_at = captured_at_by_url.get(site.url)
            # Care and fleet exports must apply identical integrity rules to
            # imported timestamps, including out-of-range UTC offsets.
            lines.append(f"Captured at: {_report_checked_at(captured_at)}")
        lines.append("")
        if escalated_by_care:
            lines += ["Paired care finding escalates this site; review the care report above.", ""]
        lines += ["Recommended actions:"]
        lines += [
            f"- [{_report_alert_text(a.severity)}] " + (
                markdown_literal(site.name) + " " + _report_alert_text(a.message[len(site.name) + 1:])
                if a.message.startswith(f"{site.name} ")
                else _report_alert_text(a.message)
            )
            for a in alerts
        ] if alerts else [
            "- Review the paired care check and resolve its findings."
            if escalated_by_care else "- Continue normal maintenance cadence."
        ]
        lines.append("")
    return "\n".join(lines).strip() + "\n"
