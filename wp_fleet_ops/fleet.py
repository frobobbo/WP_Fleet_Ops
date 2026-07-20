from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone


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
    if site.security_header_count < 2:
        score -= 6
    return max(0, min(100, score))


def generate_alerts(site: FleetSite) -> list[Alert]:
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
    if site.security_header_count < 2:
        alerts.append(Alert(site.name, "info", "Security headers need review."))
    return alerts


def generate_maintenance_report(sites: list[FleetSite]) -> str:
    lines = ["# WP FleetOps Maintenance Report", "", f"Generated: {datetime.now(timezone.utc).isoformat()}", ""]
    scored = [(s, calculate_health_score(s), generate_alerts(s)) for s in sites]
    lines += [
        f"Sites monitored: {len(sites)}",
        f"Critical sites: {sum(1 for _, _, a in scored if any(x.severity == 'critical' for x in a))}",
        "",
    ]
    for site, score, alerts in scored:
        state = "Healthy" if score >= 85 else ("Watch" if score >= 65 else "Needs attention")
        lines += [f"## {site.name} — {state}", "", f"Score: {score}/100", f"URL: {site.url}", ""]
        lines += ["Recommended actions:"]
        lines += [f"- [{a.severity}] {a.message}" for a in alerts] if alerts else ["- Continue normal maintenance cadence."]
        lines.append("")
    return "\n".join(lines).strip() + "\n"
