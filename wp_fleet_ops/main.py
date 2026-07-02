from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .checks import evaluate_site, fetch_basic_site_check, summarize_care_report
from .fleet import FleetSite, calculate_health_score, generate_alerts, generate_maintenance_report
from .storage import FleetOpsStore

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("WP_FLEET_OPS_DATA_DIR", BASE / "data"))
DB_PATH = Path(os.getenv("WP_FLEET_OPS_DB", DATA_DIR / "fleetops.sqlite3"))


def template_dir() -> Path:
    candidates = [
        os.getenv("WP_FLEET_OPS_TEMPLATE_DIR"),
        BASE / "templates",
        Path.cwd() / "templates",
        Path.cwd() / "app" / "templates",
        Path(__file__).resolve().parent / "templates",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).joinpath("index.html").exists():
            return Path(candidate)
    return BASE / "templates"


app = FastAPI(title="WP FleetOps", version="0.1.0")
templates = Jinja2Templates(directory=str(template_dir()))
store = FleetOpsStore(DB_PATH)


@app.get("/health")
def health():
    return {"status": "ok", "app": "wp-fleet-ops"}


@app.get("/ready")
def ready():
    """Confirm the app can reach its SQLite store before receiving traffic."""
    return {"status": "ready", "app": "wp-fleet-ops", "database": "ok", **store.health_counts()}


def _dashboard_status(score: int) -> str:
    return "green" if score >= 85 else ("yellow" if score >= 65 else "red")


def _recommended_action(alert: dict) -> str:
    """Translate an alert into a concise operator next step."""
    message = (alert.get("message") or "").lower()
    if "down" in message or "uptime" in message:
        return "Confirm site availability, hosting status, and recent deploys."
    if "ssl" in message or "certificate" in message:
        return "Renew or replace the TLS certificate before it expires."
    if "backup" in message:
        return "Run and verify a fresh backup, then confirm backup scheduling."
    if "wordpress updates" in message or "updates pending" in message:
        return "Schedule WordPress core, plugin, and theme updates."
    if "slow" in message or "response" in message or "latency" in message:
        return "Review performance, caching, and upstream response time."
    if "security" in message or "header" in message:
        return "Add or correct the missing security headers."
    return "Review the site dashboard and resolve the reported condition."


@app.get("/api/summary")
def api_summary():
    """Return compact dashboard rollups for automation and lightweight checks."""
    fleet_rows = store.latest_dashboard()
    care_checks = store.latest_care_checks()
    sites = store.list_sites()
    score_total = sum(row["score"] or 0 for row in fleet_rows)
    critical_alerts = sum(1 for row in fleet_rows for alert in row["alerts"] if alert.get("severity") == "critical")
    last_snapshot_at = max((row["captured_at"] for row in fleet_rows if row.get("captured_at")), default=None)
    average_score = round(score_total / len(fleet_rows)) if fleet_rows else 100
    overall_status = "green" if average_score >= 85 and critical_alerts == 0 else ("yellow" if average_score >= 65 else "red")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_status": overall_status,
        "sites": len(sites),
        "fleet_snapshots": len(fleet_rows),
        "care_checks": len(care_checks),
        "healthy_sites": sum(1 for row in fleet_rows if row["score"] >= 85),
        "needs_attention": sum(1 for row in fleet_rows if row["score"] < 70),
        "client_risks": sum(1 for check in care_checks if check["status"] == "red"),
        "critical_alerts": critical_alerts,
        "average_score": average_score,
        "last_snapshot_at": last_snapshot_at,
    }


@app.get("/api/sites")
def api_sites():
    """Return latest per-site operational status, sorted by riskiest site first."""
    return {
        "sites": [
            {
                "name": row["name"],
                "url": row["url"],
                "client": row["client"],
                "score": row["score"],
                "status": _dashboard_status(row["score"]),
                "latest_snapshot_at": row["captured_at"],
                "critical_alerts": sum(1 for alert in row["alerts"] if alert.get("severity") == "critical"),
                "alerts": row["alerts"],
            }
            for row in store.latest_dashboard()
        ]
    }


@app.get("/api/clients")
def api_clients():
    """Return account-level health rollups for client review and automation."""
    client_rows: dict[str, dict] = {}
    for row in store.latest_dashboard():
        client_name = row.get("client") or "Unassigned"
        summary = client_rows.setdefault(
            client_name,
            {
                "client": client_name,
                "site_count": 0,
                "score_total": 0,
                "healthy_sites": 0,
                "needs_attention": 0,
                "critical_alerts": 0,
                "latest_snapshot_at": None,
            },
        )
        summary["site_count"] += 1
        summary["score_total"] += row["score"] or 0
        summary["healthy_sites"] += 1 if row["score"] >= 85 else 0
        summary["needs_attention"] += 1 if row["score"] < 70 else 0
        summary["critical_alerts"] += sum(1 for alert in row["alerts"] if alert.get("severity") == "critical")
        captured_at = row.get("captured_at")
        if captured_at and (summary["latest_snapshot_at"] is None or captured_at > summary["latest_snapshot_at"]):
            summary["latest_snapshot_at"] = captured_at

    clients = []
    for summary in client_rows.values():
        average_score = round(summary.pop("score_total") / summary["site_count"]) if summary["site_count"] else 100
        summary["average_score"] = average_score
        summary["status"] = "red" if summary["critical_alerts"] else _dashboard_status(average_score)
        clients.append(summary)

    clients.sort(key=lambda row: (row["status"] != "red", row["average_score"], row["client"].lower()))
    return {"clients": clients}


def _sla_breaches(row: dict) -> list[dict]:
    """Return operational target misses for a dashboard row."""
    breaches = []
    if not row["uptime_ok"]:
        breaches.append(
            {
                "target": "availability",
                "severity": "critical",
                "observed": "down",
                "threshold": "site reachable",
                "recommended_action": "Confirm site availability, hosting status, DNS, and recent deploys.",
            }
        )
    if row["ssl_days"] < 14:
        breaches.append(
            {
                "target": "tls_certificate",
                "severity": "critical" if row["ssl_days"] <= 7 else "warning",
                "observed": f"{row['ssl_days']} days remaining",
                "threshold": ">= 14 days remaining",
                "recommended_action": "Renew or replace the TLS certificate before client traffic is at risk.",
            }
        )
    if row["backup_age_hours"] > 72:
        breaches.append(
            {
                "target": "backup_freshness",
                "severity": "critical",
                "observed": f"{row['backup_age_hours']} hours old",
                "threshold": "<= 72 hours old",
                "recommended_action": "Run and verify a fresh backup immediately.",
            }
        )
    if row["response_ms"] > 1500:
        breaches.append(
            {
                "target": "response_time",
                "severity": "warning",
                "observed": f"{row['response_ms']} ms",
                "threshold": "<= 1500 ms",
                "recommended_action": "Review caching, hosting resources, and slow page dependencies.",
            }
        )
    return breaches


@app.get("/api/sla-breaches")
def api_sla_breaches():
    """Return sites currently missing core operational service targets."""
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    sites = []
    for row in store.latest_dashboard():
        breaches = _sla_breaches(row)
        if not breaches:
            continue
        breaches.sort(key=lambda breach: (severity_rank.get(breach["severity"], 99), breach["target"]))
        sites.append(
            {
                "name": row["name"],
                "url": row["url"],
                "client": row.get("client") or "Unassigned",
                "score": row["score"],
                "breach_count": len(breaches),
                "highest_severity": breaches[0]["severity"],
                "latest_snapshot_at": row["captured_at"],
                "breaches": breaches,
            }
        )

    sites.sort(
        key=lambda site: (
            severity_rank.get(site["highest_severity"], 99),
            -site["breach_count"],
            site["score"],
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site_count": len(store.latest_dashboard()),
        "breach_count": len(sites),
        "critical_breach_count": sum(1 for site in sites if site["highest_severity"] == "critical"),
        "sites": sites,
    }


def _current_actions() -> list[dict]:
    """Build a sorted action list from the latest fleet snapshots."""
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    actions = []
    for row in store.latest_dashboard():
        for alert in row["alerts"]:
            severity = alert.get("severity", "info")
            actions.append(
                {
                    "site": row["name"],
                    "url": row["url"],
                    "client": row.get("client") or "Unassigned",
                    "score": row["score"],
                    "severity": severity,
                    "message": alert.get("message", "Review site status."),
                    "recommended_action": _recommended_action(alert),
                    "latest_snapshot_at": row["captured_at"],
                }
            )

    actions.sort(
        key=lambda action: (
            severity_rank.get(action["severity"], 99),
            action["score"],
            action["client"].lower(),
            action["site"].lower(),
        )
    )
    return actions


@app.get("/api/actions")
def api_actions():
    """Return a prioritized work queue of current fleet alerts for operators."""
    actions = _current_actions()
    return {"action_count": len(actions), "actions": actions}


def _client_workload_rows() -> list[dict]:
    """Group current open fleet actions by client for account-level triage."""
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    clients: dict[str, dict] = {}
    for action in _current_actions():
        client_name = action.get("client") or "Unassigned"
        summary = clients.setdefault(
            client_name,
            {
                "client": client_name,
                "site_names": set(),
                "open_action_count": 0,
                "critical_action_count": 0,
                "warning_action_count": 0,
                "info_action_count": 0,
                "lowest_score": action["score"],
                "latest_snapshot_at": None,
                "top_action": None,
            },
        )
        summary["site_names"].add(action["site"])
        summary["open_action_count"] += 1
        summary[f"{action['severity']}_action_count"] += 1
        summary["lowest_score"] = min(summary["lowest_score"], action["score"])
        captured_at = action.get("latest_snapshot_at")
        if captured_at and (summary["latest_snapshot_at"] is None or captured_at > summary["latest_snapshot_at"]):
            summary["latest_snapshot_at"] = captured_at
        current_top = summary["top_action"]
        if current_top is None or (
            severity_rank.get(action["severity"], 99), action["score"], action["site"].lower()
        ) < (
            severity_rank.get(current_top["severity"], 99), current_top["score"], current_top["site"].lower()
        ):
            summary["top_action"] = action

    rows = []
    for summary in clients.values():
        top_action = summary.pop("top_action")
        site_names = summary.pop("site_names")
        summary["site_count"] = len(site_names)
        summary["top_site"] = top_action["site"]
        summary["top_severity"] = top_action["severity"]
        summary["top_message"] = top_action["message"]
        summary["top_recommended_action"] = top_action["recommended_action"]
        rows.append(summary)

    rows.sort(
        key=lambda row: (
            -row["critical_action_count"],
            -row["warning_action_count"],
            -row["open_action_count"],
            row["lowest_score"],
            row["client"].lower(),
        )
    )
    return rows


@app.get("/api/client-workload")
def api_client_workload():
    """Return account-level open action counts for client triage."""
    clients = _client_workload_rows()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "client_count": len(clients),
        "open_action_count": sum(client["open_action_count"] for client in clients),
        "critical_action_count": sum(client["critical_action_count"] for client in clients),
        "clients": clients,
    }


@app.get("/api/incidents")
def api_incidents():
    """Return critical current incidents for alerting and escalation."""
    incidents = [action for action in _current_actions() if action["severity"] == "critical"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "incident_count": len(incidents),
        "affected_site_count": len({incident["site"] for incident in incidents}),
        "affected_client_count": len({incident["client"] for incident in incidents}),
        "incidents": incidents,
    }


def _backup_status(backup_age_hours: int) -> str:
    if backup_age_hours > 72:
        return "critical"
    if backup_age_hours > 36:
        return "warning"
    return "fresh"


def _backup_recommended_action(status: str) -> str:
    if status == "critical":
        return "Run and verify an immediate backup."
    if status == "warning":
        return "Confirm the next scheduled backup completes successfully."
    return "Continue normal backup monitoring."


@app.get("/api/backups")
def api_backups():
    """Return backup freshness status for each monitored site."""
    sites = []
    for row in store.latest_dashboard():
        status = _backup_status(row["backup_age_hours"])
        sites.append(
            {
                "name": row["name"],
                "url": row["url"],
                "client": row.get("client") or "Unassigned",
                "backup_age_hours": row["backup_age_hours"],
                "backup_status": status,
                "latest_snapshot_at": row["captured_at"],
                "recommended_action": _backup_recommended_action(status),
            }
        )

    sites.sort(key=lambda site: (-site["backup_age_hours"], site["client"].lower(), site["name"].lower()))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site_count": len(sites),
        "fresh_count": sum(1 for site in sites if site["backup_status"] == "fresh"),
        "stale_count": sum(1 for site in sites if site["backup_status"] != "fresh"),
        "oldest_backup_age_hours": max((site["backup_age_hours"] for site in sites), default=0),
        "sites": sites,
    }


def _security_status(security_header_count: int) -> str:
    if security_header_count >= 3:
        return "covered"
    if security_header_count >= 2:
        return "warning"
    return "critical"


def _security_recommended_action(status: str) -> str:
    if status == "critical":
        return "Add HSTS and clickjacking protection headers."
    if status == "warning":
        return "Review missing security headers and add the remaining recommended header."
    return "Continue normal security header monitoring."


@app.get("/api/security")
def api_security():
    """Return security header coverage gaps across the monitored fleet."""
    sites = []
    for row in store.latest_dashboard():
        status = _security_status(row["security_header_count"])
        sites.append(
            {
                "name": row["name"],
                "url": row["url"],
                "client": row.get("client") or "Unassigned",
                "security_header_count": row["security_header_count"],
                "security_status": status,
                "latest_snapshot_at": row["captured_at"],
                "recommended_action": _security_recommended_action(status),
            }
        )

    sites.sort(key=lambda site: (site["security_header_count"], site["client"].lower(), site["name"].lower()))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site_count": len(sites),
        "covered_count": sum(1 for site in sites if site["security_status"] == "covered"),
        "gap_count": sum(1 for site in sites if site["security_status"] != "covered"),
        "average_security_header_count": round(
            sum(site["security_header_count"] for site in sites) / len(sites), 1
        )
        if sites
        else 0,
        "sites": sites,
    }


def _performance_status(response_ms: int) -> str:
    if response_ms > 1500:
        return "slow"
    if response_ms > 750:
        return "warning"
    return "fast"


def _performance_recommended_action(status: str) -> str:
    if status == "slow":
        return "Investigate hosting, caching, and heavy checkout/page dependencies."
    if status == "warning":
        return "Review caching and frontend asset weight before it becomes a client-visible issue."
    return "Continue normal performance monitoring."


@app.get("/api/performance")
def api_performance():
    """Return response-time health across the monitored fleet, slowest first."""
    sites = []
    for row in store.latest_dashboard():
        status = _performance_status(row["response_ms"])
        sites.append(
            {
                "name": row["name"],
                "url": row["url"],
                "client": row.get("client") or "Unassigned",
                "response_ms": row["response_ms"],
                "performance_status": status,
                "latest_snapshot_at": row["captured_at"],
                "recommended_action": _performance_recommended_action(status),
            }
        )

    sites.sort(key=lambda site: (-site["response_ms"], site["client"].lower(), site["name"].lower()))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site_count": len(sites),
        "slow_count": sum(1 for site in sites if site["performance_status"] == "slow"),
        "warning_count": sum(1 for site in sites if site["performance_status"] == "warning"),
        "average_response_ms": round(sum(site["response_ms"] for site in sites) / len(sites)) if sites else 0,
        "max_response_ms": max((site["response_ms"] for site in sites), default=0),
        "sites": sites,
    }


def _certificate_status(ssl_days: int) -> str:
    if ssl_days <= 7:
        return "critical"
    if ssl_days <= 30:
        return "warning"
    return "healthy"


def _certificate_recommended_action(status: str) -> str:
    if status == "critical":
        return "Renew or replace the TLS certificate immediately."
    if status == "warning":
        return "Schedule certificate renewal before the 7-day critical window."
    return "Continue normal certificate monitoring."


@app.get("/api/certificates")
def api_certificates():
    """Return TLS certificate expiry inventory, ordered by soonest renewal need."""
    sites = []
    for row in store.latest_dashboard():
        status = _certificate_status(row["ssl_days"])
        sites.append(
            {
                "name": row["name"],
                "url": row["url"],
                "client": row.get("client") or "Unassigned",
                "ssl_days_remaining": row["ssl_days"],
                "certificate_status": status,
                "latest_snapshot_at": row["captured_at"],
                "recommended_action": _certificate_recommended_action(status),
            }
        )

    sites.sort(key=lambda site: (site["ssl_days_remaining"], site["client"].lower(), site["name"].lower()))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site_count": len(sites),
        "critical_count": sum(1 for site in sites if site["certificate_status"] == "critical"),
        "warning_count": sum(1 for site in sites if site["certificate_status"] == "warning"),
        "minimum_ssl_days": min((site["ssl_days_remaining"] for site in sites), default=None),
        "sites": sites,
    }


def _update_status(pending_updates: int) -> str:
    if pending_updates >= 5:
        return "critical"
    if pending_updates > 0:
        return "warning"
    return "current"


def _update_recommended_action(status: str) -> str:
    if status == "critical":
        return "Plan a supervised update window and backup verification before applying updates."
    if status == "warning":
        return "Schedule routine WordPress core, plugin, and theme updates."
    return "Continue normal update monitoring."


@app.get("/api/updates")
def api_updates():
    """Return WordPress update backlog inventory, ordered by largest backlog."""
    sites = []
    for row in store.latest_dashboard():
        status = _update_status(row["wp_updates"])
        sites.append(
            {
                "name": row["name"],
                "url": row["url"],
                "client": row.get("client") or "Unassigned",
                "pending_updates": row["wp_updates"],
                "update_status": status,
                "latest_snapshot_at": row["captured_at"],
                "recommended_action": _update_recommended_action(status),
            }
        )

    sites.sort(key=lambda site: (-site["pending_updates"], site["client"].lower(), site["name"].lower()))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site_count": len(sites),
        "backlog_count": sum(1 for site in sites if site["pending_updates"] > 0),
        "total_pending_updates": sum(site["pending_updates"] for site in sites),
        "max_pending_updates": max((site["pending_updates"] for site in sites), default=0),
        "sites": sites,
    }


def _maintenance_reasons(row: dict) -> list[str]:
    """Identify conditions that should be grouped into a safe work window."""
    reasons = []
    if not row["uptime_ok"]:
        reasons.append("site availability incident")
    if row["ssl_days"] <= 30:
        reasons.append("TLS certificate renewal")
    if row["wp_updates"] > 0:
        reasons.append("WordPress update backlog")
    if row["backup_age_hours"] > 36:
        reasons.append("backup freshness verification")
    if row["response_ms"] > 750:
        reasons.append("performance tuning")
    if row["security_header_count"] < 3:
        reasons.append("security header hardening")
    return reasons


def _maintenance_window(row: dict, reasons: list[str]) -> str:
    if not row["uptime_ok"] or row["ssl_days"] <= 7 or row["backup_age_hours"] > 72 or row["wp_updates"] >= 5:
        return "immediate"
    if reasons:
        return "scheduled"
    return "none"


def _maintenance_recommended_action(window: str) -> str:
    if window == "immediate":
        return "Take a verified backup, notify the client, and run an immediate supervised maintenance window."
    if window == "scheduled":
        return "Plan a routine maintenance window with backup verification and post-change smoke checks."
    return "No maintenance window is currently required."


def _risk_register_entries() -> list[dict]:
    """Group current site risks by operational category for planning reviews."""
    definitions = [
        (
            "availability",
            "Site availability incidents",
            lambda row: not row["uptime_ok"],
            lambda row: "critical",
            lambda row: "Confirm hosting availability, DNS, and recent deployment changes.",
        ),
        (
            "tls",
            "TLS certificate renewals",
            lambda row: row["ssl_days"] <= 30,
            lambda row: "critical" if row["ssl_days"] <= 7 else "warning",
            lambda row: "Renew certificates inside the current maintenance window.",
        ),
        (
            "updates",
            "WordPress update backlog",
            lambda row: row["wp_updates"] > 0,
            lambda row: "critical" if row["wp_updates"] >= 5 else "warning",
            lambda row: "Apply WordPress core, plugin, and theme updates after backup verification.",
        ),
        (
            "backups",
            "Backup freshness gaps",
            lambda row: row["backup_age_hours"] > 36,
            lambda row: "critical" if row["backup_age_hours"] > 72 else "warning",
            lambda row: "Run and verify fresh backups before any site changes.",
        ),
        (
            "performance",
            "Homepage performance degradation",
            lambda row: row["response_ms"] > 750,
            lambda row: "critical" if row["response_ms"] > 2500 else "warning",
            lambda row: "Review caching, hosting resources, and slow page dependencies.",
        ),
        (
            "security",
            "Security header coverage gaps",
            lambda row: row["security_header_count"] < 3,
            lambda row: "critical" if row["security_header_count"] < 2 else "warning",
            lambda row: "Add missing HSTS, clickjacking, or content security headers.",
        ),
    ]
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    entries = []
    for key, label, predicate, severity_for, action_for in definitions:
        affected_sites = []
        for row in store.latest_dashboard():
            if not predicate(row):
                continue
            affected_sites.append(
                {
                    "name": row["name"],
                    "url": row["url"],
                    "client": row.get("client") or "Unassigned",
                    "score": row["score"],
                    "severity": severity_for(row),
                    "recommended_action": action_for(row),
                    "latest_snapshot_at": row["captured_at"],
                }
            )
        if not affected_sites:
            continue
        affected_sites.sort(
            key=lambda site: (
                severity_rank.get(site["severity"], 99),
                site["score"],
                site["client"].lower(),
                site["name"].lower(),
            )
        )
        entries.append(
            {
                "category": key,
                "label": label,
                "affected_site_count": len(affected_sites),
                "highest_severity": affected_sites[0]["severity"],
                "sites": affected_sites,
            }
        )
    entries.sort(key=lambda entry: (severity_rank.get(entry["highest_severity"], 99), -entry["affected_site_count"], entry["category"]))
    return entries


@app.get("/api/risk-register")
def api_risk_register():
    """Return category-level operational risks for client planning and QBRs."""
    entries = _risk_register_entries()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "category_count": len(entries),
        "critical_category_count": sum(1 for entry in entries if entry["highest_severity"] == "critical"),
        "entries": entries,
    }


@app.get("/api/maintenance-windows")
def api_maintenance_windows():
    """Return sites that need grouped maintenance work, prioritized by urgency."""
    urgency_rank = {"immediate": 0, "scheduled": 1, "none": 2}
    sites = []
    for row in store.latest_dashboard():
        reasons = _maintenance_reasons(row)
        window = _maintenance_window(row, reasons)
        if window == "none":
            continue
        sites.append(
            {
                "name": row["name"],
                "url": row["url"],
                "client": row.get("client") or "Unassigned",
                "score": row["score"],
                "maintenance_window": window,
                "risk_count": len(reasons),
                "reasons": reasons,
                "latest_snapshot_at": row["captured_at"],
                "recommended_action": _maintenance_recommended_action(window),
            }
        )

    sites.sort(key=lambda site: (urgency_rank[site["maintenance_window"]], -site["risk_count"], site["score"], site["client"].lower(), site["name"].lower()))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site_count": len(store.latest_dashboard()),
        "window_count": len(sites),
        "immediate_count": sum(1 for site in sites if site["maintenance_window"] == "immediate"),
        "scheduled_count": sum(1 for site in sites if site["maintenance_window"] == "scheduled"),
        "sites": sites,
    }


def _slo_status(met: int, total: int) -> str:
    """Return a compact status label for an SLO compliance ratio."""
    if total == 0:
        return "unknown"
    ratio = met / total
    if ratio >= 0.95:
        return "healthy"
    if ratio >= 0.80:
        return "watch"
    return "at_risk"


def _slo_row(name: str, label: str, total: int, met: int, threshold: str) -> dict:
    compliance_percent = round((met / total) * 100, 1) if total else 100.0
    return {
        "name": name,
        "label": label,
        "threshold": threshold,
        "met_count": met,
        "miss_count": max(total - met, 0),
        "compliance_percent": compliance_percent,
        "status": _slo_status(met, total),
    }


def _maintenance_calendar_windows() -> list[dict]:
    """Summarize maintenance work by timing window for planning views."""
    labels = {"immediate": "Immediate maintenance", "scheduled": "Scheduled maintenance"}
    urgency_rank = {"immediate": 0, "scheduled": 1}
    windows: dict[str, dict] = {}
    for row in store.latest_dashboard():
        reasons = _maintenance_reasons(row)
        window = _maintenance_window(row, reasons)
        if window == "none":
            continue
        site = {
            "name": row["name"],
            "url": row["url"],
            "client": row.get("client") or "Unassigned",
            "score": row["score"],
            "risk_count": len(reasons),
            "reasons": reasons,
            "latest_snapshot_at": row["captured_at"],
        }
        summary = windows.setdefault(
            window,
            {
                "window": window,
                "label": labels[window],
                "site_count": 0,
                "client_names": set(),
                "total_risk_count": 0,
                "recommended_action": _maintenance_recommended_action(window),
                "sites": [],
            },
        )
        summary["site_count"] += 1
        summary["client_names"].add(site["client"])
        summary["total_risk_count"] += site["risk_count"]
        summary["sites"].append(site)

    rows = []
    for summary in windows.values():
        summary["sites"].sort(key=lambda site: (-site["risk_count"], site["score"], site["client"].lower(), site["name"].lower()))
        summary["client_count"] = len(summary.pop("client_names"))
        summary["top_site"] = summary["sites"][0]["name"] if summary["sites"] else None
        rows.append(summary)
    rows.sort(key=lambda row: (urgency_rank[row["window"]], -row["total_risk_count"], row["window"]))
    return rows


@app.get("/api/maintenance-calendar")
def api_maintenance_calendar():
    """Return maintenance work grouped by immediate vs scheduled windows."""
    windows = _maintenance_calendar_windows()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site_count": len(store.latest_dashboard()),
        "window_count": len(windows),
        "immediate_site_count": sum(window["site_count"] for window in windows if window["window"] == "immediate"),
        "scheduled_site_count": sum(window["site_count"] for window in windows if window["window"] == "scheduled"),
        "windows": windows,
    }


@app.get("/api/slo")
def api_slo():
    """Return fleet-level service objective compliance for leadership review."""
    rows = store.latest_dashboard()
    total = len(rows)
    objectives = [
        _slo_row("availability", "Sites reachable", total, sum(1 for row in rows if row["uptime_ok"]), "site reachable"),
        _slo_row("tls", "TLS renewal buffer", total, sum(1 for row in rows if row["ssl_days"] >= 14), ">= 14 days remaining"),
        _slo_row("backups", "Backup freshness", total, sum(1 for row in rows if row["backup_age_hours"] <= 72), "<= 72 hours old"),
        _slo_row("performance", "Homepage response", total, sum(1 for row in rows if row["response_ms"] <= 1500), "<= 1500 ms"),
        _slo_row("security", "Security headers", total, sum(1 for row in rows if row["security_header_count"] >= 2), ">= 2 core headers"),
    ]
    objectives.sort(key=lambda objective: (objective["compliance_percent"], objective["name"]))
    worst_objective = objectives[0] if objectives else None
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site_count": total,
        "objective_count": len(objectives),
        "at_risk_count": sum(1 for objective in objectives if objective["status"] == "at_risk"),
        "worst_objective": worst_objective,
        "objectives": objectives,
    }


def _remediation_bucket(action: dict) -> str:
    """Map an open action to a practical operator timing bucket."""
    if action["severity"] == "critical":
        return "immediate"
    if action["severity"] == "warning":
        return "scheduled"
    return "watch"


def _remediation_due(bucket: str) -> str:
    if bucket == "immediate":
        return "today"
    if bucket == "scheduled":
        return "next maintenance window"
    return "monitoring review"


@app.get("/api/remediation-plan")
def api_remediation_plan():
    """Return current fleet actions grouped into operator timing buckets."""
    labels = {
        "immediate": "Immediate remediation",
        "scheduled": "Scheduled maintenance",
        "watch": "Monitoring watchlist",
    }
    buckets: dict[str, list[dict]] = {"immediate": [], "scheduled": [], "watch": []}
    for action in _current_actions():
        bucket = _remediation_bucket(action)
        buckets[bucket].append({**action, "due": _remediation_due(bucket)})

    bucket_rows = [
        {
            "bucket": bucket,
            "label": labels[bucket],
            "action_count": len(actions),
            "actions": actions,
        }
        for bucket, actions in buckets.items()
        if actions
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "action_count": sum(len(actions) for actions in buckets.values()),
        "immediate_count": len(buckets["immediate"]),
        "scheduled_count": len(buckets["scheduled"]),
        "watch_count": len(buckets["watch"]),
        "buckets": bucket_rows,
    }


def _client_digest_status(immediate_count: int, scheduled_count: int, average_score: int) -> str:
    """Return a client-friendly status for an account digest."""
    if immediate_count:
        return "red"
    if scheduled_count or average_score < 85:
        return "yellow"
    return "green"


@app.get("/api/client-digest")
def api_client_digest():
    """Return client-level executive summaries for account check-ins."""
    actions_by_client: dict[str, list[dict]] = {}
    for action in _current_actions():
        actions_by_client.setdefault(action.get("client") or "Unassigned", []).append(action)

    clients: dict[str, dict] = {}
    for row in store.latest_dashboard():
        client_name = row.get("client") or "Unassigned"
        digest = clients.setdefault(
            client_name,
            {
                "client": client_name,
                "site_count": 0,
                "score_total": 0,
                "sites": [],
                "latest_snapshot_at": None,
            },
        )
        digest["site_count"] += 1
        digest["score_total"] += row["score"] or 0
        digest["sites"].append(
            {
                "name": row["name"],
                "url": row["url"],
                "score": row["score"],
                "status": _dashboard_status(row["score"]),
                "critical_alerts": sum(1 for alert in row["alerts"] if alert.get("severity") == "critical"),
            }
        )
        captured_at = row.get("captured_at")
        if captured_at and (digest["latest_snapshot_at"] is None or captured_at > digest["latest_snapshot_at"]):
            digest["latest_snapshot_at"] = captured_at

    digest_rows = []
    for client_name, digest in clients.items():
        actions = actions_by_client.get(client_name, [])
        immediate_count = sum(1 for action in actions if _remediation_bucket(action) == "immediate")
        scheduled_count = sum(1 for action in actions if _remediation_bucket(action) == "scheduled")
        watch_count = sum(1 for action in actions if _remediation_bucket(action) == "watch")
        average_score = round(digest.pop("score_total") / digest["site_count"]) if digest["site_count"] else 100
        top_action = actions[0] if actions else None
        status = _client_digest_status(immediate_count, scheduled_count, average_score)
        digest["average_score"] = average_score
        digest["status"] = status
        digest["immediate_action_count"] = immediate_count
        digest["scheduled_action_count"] = scheduled_count
        digest["watch_action_count"] = watch_count
        digest["open_action_count"] = len(actions)
        digest["top_site"] = top_action["site"] if top_action else None
        digest["top_message"] = top_action["message"] if top_action else "No open fleet actions."
        digest["executive_summary"] = (
            f"{client_name} has {digest['site_count']} tracked site"
            f"{'s' if digest['site_count'] != 1 else ''}, an average score of {average_score}, "
            f"and {len(actions)} open action{'s' if len(actions) != 1 else ''}."
        )
        digest["sites"].sort(key=lambda site: (site["score"], site["name"].lower()))
        digest_rows.append(digest)

    digest_rows.sort(
        key=lambda row: (
            {"red": 0, "yellow": 1, "green": 2}.get(row["status"], 99),
            -row["immediate_action_count"],
            -row["scheduled_action_count"],
            row["average_score"],
            row["client"].lower(),
        )
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "client_count": len(digest_rows),
        "red_count": sum(1 for client in digest_rows if client["status"] == "red"),
        "yellow_count": sum(1 for client in digest_rows if client["status"] == "yellow"),
        "green_count": sum(1 for client in digest_rows if client["status"] == "green"),
        "clients": digest_rows,
    }


def _parse_captured_at(value: str | None) -> datetime | None:
    """Parse SQLite or ISO timestamps into timezone-aware UTC datetimes."""
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@app.get("/api/stale-snapshots")
def api_stale_snapshots(threshold_hours: int = 168):
    """Return sites whose latest fleet snapshot is missing or older than the threshold."""
    threshold_hours = max(threshold_hours, 1)
    now = datetime.now(timezone.utc)
    all_sites = store.list_sites()
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    sites = []
    for site in all_sites:
        row = latest_by_url.get(site["url"])
        captured_at = row.get("captured_at") if row else None
        captured_dt = _parse_captured_at(captured_at)
        age_hours = round((now - captured_dt).total_seconds() / 3600, 1) if captured_dt else None
        if age_hours is not None and age_hours <= threshold_hours:
            continue
        sites.append(
            {
                "name": site["name"],
                "url": site["url"],
                "client": site.get("client") or "Unassigned",
                "latest_snapshot_at": captured_at,
                "snapshot_age_hours": age_hours,
                "staleness_status": "missing" if row is None else "stale",
                "recommended_action": "Capture a fresh fleet snapshot and verify site health.",
            }
        )

    sites.sort(
        key=lambda site: (
            site["staleness_status"] != "missing",
            -(site["snapshot_age_hours"] or 10**9),
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    return {
        "generated_at": now.isoformat(),
        "threshold_hours": threshold_hours,
        "site_count": len(all_sites),
        "stale_count": len(sites),
        "missing_snapshot_count": sum(1 for site in sites if site["staleness_status"] == "missing"),
        "sites": sites,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"fleet_rows": store.latest_dashboard(), "care_checks": store.latest_care_checks(), "sites": store.list_sites()},
    )


@app.post("/sites")
def add_site(name: str = Form(...), url: str = Form(...), client: str = Form("")):
    store.upsert_site(name, url, client)
    return RedirectResponse("/", status_code=303)


@app.post("/care/manual-check")
def manual_care_check(
    name: str = Form(..., min_length=1),
    url: str = Form(..., min_length=1, pattern=r"^https?://"),
    client: str = Form(""),
    http_status: int = Form(200, ge=100, le=599),
    latency_ms: int = Form(250, ge=0),
    ssl_days_remaining: int = Form(60, ge=0),
    wordpress_version: str = Form("unknown"),
    update_count: int = Form(0, ge=0),
    backup_age_hours: int = Form(24, ge=0),
):
    site_id = store.upsert_site(name, url, client)
    check = evaluate_site(
        name,
        url,
        http_status,
        latency_ms,
        ssl_days_remaining,
        wordpress_version,
        update_count,
        backup_age_hours,
        {},
    )
    store.save_care_check(site_id, check)
    fleet_site = FleetSite(name, check.url, http_status < 400 and http_status >= 200, ssl_days_remaining, update_count, backup_age_hours, latency_ms, 0)
    store.save_snapshot(site_id, fleet_site, calculate_health_score(fleet_site), generate_alerts(fleet_site))
    return RedirectResponse("/", status_code=303)


@app.post("/care/fetch-check")
def fetch_care_check(name: str = Form(...), url: str = Form(...), client: str = Form("")):
    site_id = store.upsert_site(name, url, client)
    check = fetch_basic_site_check(name, url)
    store.save_care_check(site_id, check)
    security_header_count = sum(
        1
        for header in ("strict-transport-security", "x-frame-options", "content-security-policy")
        if header in check.security_headers
    )
    fleet_site = FleetSite(
        check.name,
        check.url,
        200 <= check.http_status < 400,
        check.ssl_days_remaining,
        check.update_count,
        check.backup_age_hours,
        check.latency_ms,
        security_header_count,
    )
    store.save_snapshot(site_id, fleet_site, calculate_health_score(fleet_site), generate_alerts(fleet_site))
    return RedirectResponse("/", status_code=303)


@app.post("/snapshot")
def snapshot(
    name: str = Form(..., min_length=1),
    url: str = Form(..., min_length=1, pattern=r"^https?://"),
    client: str = Form(""),
    uptime_ok: bool = Form(True),
    ssl_days: int = Form(60, ge=0),
    wp_updates: int = Form(0, ge=0),
    backup_age_hours: int = Form(24, ge=0),
    response_ms: int = Form(250, ge=0),
    security_header_count: int = Form(3, ge=0),
):
    site = FleetSite(name, url, uptime_ok, ssl_days, wp_updates, backup_age_hours, response_ms, security_header_count)
    site_id = store.upsert_site(name, url, client)
    store.save_snapshot(site_id, site, calculate_health_score(site), generate_alerts(site))
    check = evaluate_site(name, url, 200 if uptime_ok else 0, response_ms, ssl_days, "unknown", wp_updates, backup_age_hours, {})
    store.save_care_check(site_id, check)
    return RedirectResponse("/", status_code=303)


def _build_text_report() -> tuple[str, int, int]:
    care_checks = [
        evaluate_site(
            r["name"],
            r["url"],
            r["http_status"],
            r["latency_ms"],
            r["ssl_days_remaining"],
            r["wordpress_version"],
            r["update_count"],
            r["backup_age_hours"],
            {},
        )
        for r in store.latest_care_checks()
    ]
    fleet_sites = [
        FleetSite(
            r["name"],
            r["url"],
            bool(r["uptime_ok"]),
            r["ssl_days"],
            r["wp_updates"],
            r["backup_age_hours"],
            r["response_ms"],
            r["security_header_count"],
        )
        for r in store.latest_dashboard()
    ]
    report_text = summarize_care_report(care_checks) + "\n---\n\n" + generate_maintenance_report(fleet_sites)
    return report_text, len(care_checks), len(fleet_sites)


@app.get("/api/report")
def api_report():
    """Return the combined care/fleet report with metadata for integrations."""
    report_text, care_check_count, site_count = _build_text_report()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site_count": site_count,
        "care_check_count": care_check_count,
        "line_count": len(report_text.splitlines()),
        "report": report_text,
    }


@app.get("/report", response_class=PlainTextResponse)
def report():
    report_text, _, _ = _build_text_report()
    return report_text


def run():
    import uvicorn

    uvicorn.run("wp_fleet_ops.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
