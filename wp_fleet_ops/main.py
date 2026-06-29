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
