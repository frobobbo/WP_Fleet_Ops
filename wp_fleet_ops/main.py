from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .checks import (
    SiteCheck,
    evaluate_site,
    fetch_basic_site_check,
    normalize_site_name,
    normalize_site_url,
    summarize_care_report,
)
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

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "connect-src 'self'"
)
SNAPSHOT_FRESHNESS_HOURS = 168
# SQLite INTEGER columns store signed 64-bit values. Reject larger telemetry at
# request validation so persistence cannot fail after a site has been upserted.
SQLITE_INTEGER_MAX = (1 << 63) - 1


@app.middleware("http")
async def add_browser_security_headers(request: Request, call_next):
    """Apply baseline browser protections to dashboard and API responses."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@app.exception_handler(ValueError)
async def invalid_input_error(_request: Request, exc: ValueError):
    """Return a client error for invalid normalized inputs instead of a server error."""
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.get("/health")
def health():
    return {"status": "ok", "app": "wp-fleet-ops"}


@app.get("/ready")
def ready():
    """Confirm the app can reach its SQLite store before receiving traffic."""
    try:
        counts = store.health_counts()
    except sqlite3.Error:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "app": "wp-fleet-ops",
                "database": "unavailable",
            },
        )
    return {"status": "ready", "app": "wp-fleet-ops", "database": "ok", **counts}


def _dashboard_status(score: int) -> str:
    return "green" if score >= 85 else ("yellow" if score >= 65 else "red")


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


def _snapshot_freshness(
    captured_at: str | None,
    now: datetime,
    threshold_hours: int,
) -> tuple[str, float | None]:
    """Return a freshness label and age for a persisted snapshot timestamp."""
    captured_dt = _parse_captured_at(captured_at)
    if captured_dt is None:
        return "invalid", None
    raw_age_hours = (now - captured_dt).total_seconds() / 3600
    age_hours = round(raw_age_hours, 1)
    if raw_age_hours < 0:
        return "clock_skew", age_hours
    if raw_age_hours > threshold_hours:
        return "stale", age_hours
    return "current", age_hours


def _snapshot_is_current(captured_at: str | None, now: datetime, threshold_hours: int) -> bool:
    """Return whether a snapshot timestamp is valid and inside its freshness window."""
    freshness, _ = _snapshot_freshness(captured_at, now, threshold_hours)
    return freshness == "current"


def _current_snapshot_rows(rows: list[dict], now: datetime | None = None) -> list[dict]:
    """Return only rows safe to use for current health and dispatch decisions."""
    observed_at = now or datetime.now(timezone.utc)
    return [
        row
        for row in rows
        if _snapshot_is_current(row.get("captured_at"), observed_at, SNAPSHOT_FRESHNESS_HOURS)
    ]


def _normalize_client_filter(client: str | None) -> str | None:
    """Normalize an optional account filter while preserving stored client names."""
    if client is None:
        return None
    normalized = client.strip()
    if not normalized:
        raise ValueError("Client filter must not be empty.")
    return "Unassigned" if normalized.casefold() == "unassigned" else normalized


def _bounded_history_offset(limit: int, offset: int, total_count: int) -> int:
    """Clamp an offset to a page that exists in the result set."""
    if total_count == 0:
        return 0
    requested_offset = max(offset, 0)
    if requested_offset >= total_count:
        return ((total_count - 1) // limit) * limit
    return requested_offset


def _pagination_navigation(limit: int, offset: int, returned_count: int, total_count: int) -> dict:
    """Return navigation, page position, and exact result-range metadata."""
    range_end = offset + returned_count
    has_more = range_end < total_count
    has_results = total_count > 0
    return {
        "has_more": has_more,
        "page_number": (offset // limit) + 1 if has_results else 0,
        "page_count": ((total_count - 1) // limit) + 1 if has_results else 0,
        "range_start": offset + 1 if returned_count else None,
        "range_end": range_end if returned_count else None,
        "remaining_count": max(total_count - range_end, 0),
        "first_offset": 0 if has_results else None,
        "last_offset": ((total_count - 1) // limit) * limit if has_results else None,
        "previous_offset": max(0, offset - limit) if offset > 0 else None,
        "next_offset": range_end if has_more else None,
    }


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
    now = datetime.now(timezone.utc)
    fleet_rows = store.latest_dashboard()
    current_rows = _current_snapshot_rows(fleet_rows, now)
    care_checks = store.latest_care_checks()
    current_care_checks = [
        check
        for check in care_checks
        if _snapshot_is_current(check.get("checked_at"), now, SNAPSHOT_FRESHNESS_HOURS)
    ]
    sites = store.list_sites()
    monitored_urls = {row["url"] for row in fleet_rows}
    monitored_site_count = sum(1 for site in sites if site["url"] in monitored_urls)
    missing_snapshot_count = len(sites) - monitored_site_count
    monitoring_coverage_percent = round((monitored_site_count / len(sites)) * 100) if sites else 100
    current_snapshot_count = len(current_rows)
    stale_snapshot_count = len(fleet_rows) - current_snapshot_count
    snapshot_freshness_percent = round((current_snapshot_count / len(sites)) * 100) if sites else 100
    care_check_urls = {check["url"] for check in care_checks}
    monitored_care_check_count = sum(1 for site in sites if site["url"] in care_check_urls)
    missing_care_check_count = len(sites) - monitored_care_check_count
    current_care_check_count = len(current_care_checks)
    stale_care_check_count = len(care_checks) - current_care_check_count
    care_check_freshness_percent = round((current_care_check_count / len(sites)) * 100) if sites else 100
    score_total = sum(row["score"] or 0 for row in current_rows)
    critical_alerts = sum(
        1
        for row in current_rows
        for alert in row["alerts"]
        if alert.get("severity") == "critical"
    )
    last_snapshot_at = max((row["captured_at"] for row in fleet_rows if row.get("captured_at")), default=None)
    average_score = round(score_total / len(current_rows)) if current_rows else 100
    if critical_alerts or average_score < 65:
        overall_status = "red"
    elif (
        missing_snapshot_count
        or stale_snapshot_count
        or missing_care_check_count
        or stale_care_check_count
        or average_score < 85
    ):
        overall_status = "yellow"
    else:
        overall_status = "green"
    return {
        "generated_at": now.isoformat(),
        "overall_status": overall_status,
        "sites": len(sites),
        "fleet_snapshots": len(fleet_rows),
        "monitored_site_count": monitored_site_count,
        "missing_snapshot_count": missing_snapshot_count,
        "monitoring_coverage_percent": monitoring_coverage_percent,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "current_snapshot_count": current_snapshot_count,
        "stale_snapshot_count": stale_snapshot_count,
        "snapshot_freshness_percent": snapshot_freshness_percent,
        "care_checks": len(care_checks),
        "monitored_care_check_count": monitored_care_check_count,
        "missing_care_check_count": missing_care_check_count,
        "current_care_check_count": current_care_check_count,
        "stale_care_check_count": stale_care_check_count,
        "care_check_freshness_percent": care_check_freshness_percent,
        "healthy_sites": sum(1 for row in current_rows if row["score"] >= 85),
        "needs_attention": sum(1 for row in current_rows if _dashboard_status(row["score"]) == "red"),
        "client_risks": sum(1 for check in current_care_checks if check["status"] == "red"),
        "critical_alerts": critical_alerts,
        "average_score": average_score,
        "last_snapshot_at": last_snapshot_at,
    }


@app.get("/api/sites")
def api_sites():
    """Return latest per-site operational status, sorted by riskiest site first."""
    now = datetime.now(timezone.utc)
    sites = []
    for row in store.latest_dashboard():
        freshness, age_hours = _snapshot_freshness(
            row.get("captured_at"),
            now,
            SNAPSHOT_FRESHNESS_HOURS,
        )
        observed_status = _dashboard_status(row["score"])
        observed_critical_alerts = sum(
            1 for alert in row["alerts"] if alert.get("severity") == "critical"
        )
        sites.append(
            {
                "name": row["name"],
                "url": row["url"],
                "client": row["client"],
                "score": row["score"],
                "status": observed_status if freshness == "current" else "unknown",
                "observed_status": observed_status,
                "latest_snapshot_at": row["captured_at"],
                "snapshot_freshness": freshness,
                "snapshot_age_hours": age_hours,
                "critical_alerts": observed_critical_alerts if freshness == "current" else 0,
                "observed_critical_alerts": observed_critical_alerts,
                "alerts": row["alerts"],
            }
        )
    return {
        "generated_at": now.isoformat(),
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "sites": sites,
    }


@app.get("/api/site-directory")
def api_site_directory():
    """Return every tracked site, including sites not yet covered by a snapshot."""
    now = datetime.now(timezone.utc)
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    sites = []
    for site in store.list_sites():
        latest = latest_by_url.get(site["url"])
        if latest:
            score = latest["score"]
            observed_status = _dashboard_status(score)
            freshness, age_hours = _snapshot_freshness(
                latest.get("captured_at"),
                now,
                SNAPSHOT_FRESHNESS_HOURS,
            )
            if freshness == "stale":
                recommended_action = "Capture a fresh fleet snapshot and verify site health."
            elif freshness == "clock_skew":
                recommended_action = "Correct the snapshot timestamp or source clock, then capture a fresh snapshot."
            elif freshness == "invalid":
                recommended_action = "Repair the invalid snapshot timestamp, then capture a fresh snapshot."
            else:
                recommended_action = (
                    "Continue normal monitoring cadence."
                    if score >= 85
                    else "Review the latest snapshot and open remediation tasks."
                )
            sites.append(
                {
                    "name": site["name"],
                    "url": site["url"],
                    "client": site["client"] or "Unassigned",
                    "monitoring_status": "monitored",
                    "score": score,
                    "status": observed_status if freshness == "current" else "unknown",
                    "observed_status": observed_status,
                    "latest_snapshot_at": latest["captured_at"],
                    "snapshot_freshness": freshness,
                    "snapshot_age_hours": age_hours,
                    "recommended_action": recommended_action,
                }
            )
        else:
            sites.append(
                {
                    "name": site["name"],
                    "url": site["url"],
                    "client": site["client"] or "Unassigned",
                    "monitoring_status": "missing_snapshot",
                    "score": None,
                    "status": "unknown",
                    "observed_status": None,
                    "latest_snapshot_at": None,
                    "snapshot_freshness": "missing",
                    "snapshot_age_hours": None,
                    "recommended_action": "Capture an initial fleet snapshot for this site.",
                }
            )
    sites.sort(key=lambda row: (row["monitoring_status"] != "missing_snapshot", row["client"], row["name"]))
    return {
        "generated_at": now.isoformat(),
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": len(sites),
        "monitored_count": sum(1 for site in sites if site["monitoring_status"] == "monitored"),
        "missing_snapshot_count": sum(1 for site in sites if site["monitoring_status"] == "missing_snapshot"),
        "current_snapshot_count": sum(1 for site in sites if site["snapshot_freshness"] == "current"),
        "stale_snapshot_count": sum(
            1 for site in sites if site["snapshot_freshness"] in {"stale", "clock_skew", "invalid"}
        ),
        "sites": sites,
    }


def _monitoring_coverage_action(snapshot_freshness: str, care_check_freshness: str) -> str:
    """Return the minimum next step needed to restore paired site-health evidence."""
    if snapshot_freshness == "current" and care_check_freshness == "current":
        return "Continue normal monitoring cadence."
    if snapshot_freshness == "missing" and care_check_freshness == "missing":
        return "Capture an initial combined care check and fleet snapshot."

    snapshot_action = (
        "an initial fleet snapshot"
        if snapshot_freshness == "missing"
        else "a fresh fleet snapshot"
    )
    care_check_action = (
        "an initial care check"
        if care_check_freshness == "missing"
        else "a fresh care check"
    )
    if snapshot_freshness == "current":
        return f"Capture {care_check_action} before relying on site health."
    if care_check_freshness == "current":
        return f"Capture {snapshot_action} before relying on site health."
    return (
        f"Capture {snapshot_action} and {care_check_action} "
        "before relying on site health."
    )


@app.get("/api/monitoring-coverage")
def api_monitoring_coverage(client: str | None = None):
    """Return paired fleet and care evidence, optionally scoped to one client."""
    now = datetime.now(timezone.utc)
    normalized_client = _normalize_client_filter(client)
    tracked_sites = [
        site
        for site in store.list_sites()
        if normalized_client is None
        or (site.get("client") or "Unassigned") == normalized_client
    ]
    if normalized_client is not None and not tracked_sites:
        return JSONResponse(
            status_code=404,
            content={
                "detail": f"No tracked sites found for client '{normalized_client}'.",
                "client": normalized_client,
            },
        )
    snapshots_by_url = {row["url"]: row for row in store.latest_dashboard()}
    care_checks_by_url = {row["url"]: row for row in store.latest_care_checks()}
    sites = []
    for site in tracked_sites:
        snapshot = snapshots_by_url.get(site["url"])
        care_check = care_checks_by_url.get(site["url"])
        snapshot_freshness, snapshot_age_hours = (
            _snapshot_freshness(snapshot.get("captured_at"), now, SNAPSHOT_FRESHNESS_HOURS)
            if snapshot
            else ("missing", None)
        )
        care_check_freshness, care_check_age_hours = (
            _snapshot_freshness(care_check.get("checked_at"), now, SNAPSHOT_FRESHNESS_HOURS)
            if care_check
            else ("missing", None)
        )
        coverage_status = (
            "current"
            if snapshot_freshness == "current" and care_check_freshness == "current"
            else "gap"
        )
        sites.append(
            {
                "name": site["name"],
                "url": site["url"],
                "client": site.get("client") or "Unassigned",
                "coverage_status": coverage_status,
                "snapshot_freshness": snapshot_freshness,
                "snapshot_age_hours": snapshot_age_hours,
                "latest_snapshot_at": snapshot.get("captured_at") if snapshot else None,
                "care_check_freshness": care_check_freshness,
                "care_check_age_hours": care_check_age_hours,
                "latest_care_check_at": care_check.get("checked_at") if care_check else None,
                "recommended_action": _monitoring_coverage_action(
                    snapshot_freshness,
                    care_check_freshness,
                ),
            }
        )

    sites.sort(
        key=lambda site: (
            site["coverage_status"] == "current",
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    current_evidence_count = sum(1 for site in sites if site["coverage_status"] == "current")
    snapshot_current_count = sum(1 for site in sites if site["snapshot_freshness"] == "current")
    care_check_current_count = sum(1 for site in sites if site["care_check_freshness"] == "current")
    tracked_site_count = len(sites)
    monitoring_gap_count = tracked_site_count - current_evidence_count
    return {
        "generated_at": now.isoformat(),
        "client": normalized_client,
        "status": "yellow" if monitoring_gap_count else "green",
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "tracked_site_count": tracked_site_count,
        "current_evidence_count": current_evidence_count,
        "monitoring_gap_count": monitoring_gap_count,
        "snapshot_current_count": snapshot_current_count,
        "snapshot_gap_count": tracked_site_count - snapshot_current_count,
        "care_check_current_count": care_check_current_count,
        "care_check_gap_count": tracked_site_count - care_check_current_count,
        "combined_coverage_percent": (
            round((current_evidence_count / tracked_site_count) * 100) if tracked_site_count else 100
        ),
        "sites": sites,
    }


@app.get("/api/clients")
def api_clients():
    """Return account-level health and monitoring coverage rollups."""
    now = datetime.now(timezone.utc)
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    client_rows: dict[str, dict] = {}
    for site in store.list_sites():
        client_name = site.get("client") or "Unassigned"
        summary = client_rows.setdefault(
            client_name,
            {
                "client": client_name,
                "site_count": 0,
                "monitored_site_count": 0,
                "current_snapshot_count": 0,
                "missing_snapshot_count": 0,
                "stale_snapshot_count": 0,
                "score_total": 0,
                "healthy_sites": 0,
                "needs_attention": 0,
                "critical_alerts": 0,
                "latest_snapshot_at": None,
                "_latest_snapshot_dt": None,
            },
        )
        summary["site_count"] += 1
        row = latest_by_url.get(site["url"])
        if row is None:
            summary["missing_snapshot_count"] += 1
            continue

        summary["monitored_site_count"] += 1
        freshness, _ = _snapshot_freshness(row.get("captured_at"), now, SNAPSHOT_FRESHNESS_HOURS)
        if freshness == "current":
            summary["current_snapshot_count"] += 1
            summary["score_total"] += row["score"] or 0
            summary["healthy_sites"] += 1 if row["score"] >= 85 else 0
            summary["needs_attention"] += 1 if _dashboard_status(row["score"]) == "red" else 0
            summary["critical_alerts"] += sum(
                1 for alert in row["alerts"] if alert.get("severity") == "critical"
            )
        else:
            summary["stale_snapshot_count"] += 1
        captured_dt = _parse_captured_at(row.get("captured_at"))
        if captured_dt and (summary["_latest_snapshot_dt"] is None or captured_dt > summary["_latest_snapshot_dt"]):
            summary["_latest_snapshot_dt"] = captured_dt
            summary["latest_snapshot_at"] = row["captured_at"]

    clients = []
    for summary in client_rows.values():
        monitored_site_count = summary["monitored_site_count"]
        current_snapshot_count = summary["current_snapshot_count"]
        average_score = round(summary.pop("score_total") / current_snapshot_count) if current_snapshot_count else 100
        summary.pop("_latest_snapshot_dt")
        summary["average_score"] = average_score
        summary["monitoring_coverage_percent"] = round(
            (monitored_site_count / summary["site_count"]) * 100
        ) if summary["site_count"] else 100
        summary["snapshot_freshness_percent"] = round(
            (summary["current_snapshot_count"] / summary["site_count"]) * 100
        ) if summary["site_count"] else 100
        monitoring_gap_count = summary["missing_snapshot_count"] + summary["stale_snapshot_count"]
        if summary["critical_alerts"] or average_score < 65:
            summary["status"] = "red"
        elif monitoring_gap_count or average_score < 85:
            summary["status"] = "yellow"
        else:
            summary["status"] = "green"
        clients.append(summary)

    status_rank = {"red": 0, "yellow": 1, "green": 2}
    clients.sort(key=lambda row: (status_rank[row["status"]], row["average_score"], row["client"].lower()))
    return {
        "generated_at": now.isoformat(),
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "client_count": len(clients),
        "missing_snapshot_count": sum(client["missing_snapshot_count"] for client in clients),
        "stale_snapshot_count": sum(client["stale_snapshot_count"] for client in clients),
        "clients": clients,
    }


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


def _sla_evidence_recommended_action(freshness: str) -> str:
    """Return a next step when SLA evidence cannot be treated as current."""
    if freshness == "missing":
        return "Capture an initial fleet snapshot before evaluating SLA compliance."
    if freshness == "clock_skew":
        return "Correct the snapshot timestamp or source clock before evaluating SLA compliance."
    if freshness == "invalid":
        return "Repair the invalid snapshot timestamp before evaluating SLA compliance."
    return "Capture a fresh fleet snapshot before evaluating SLA compliance."


@app.get("/api/sla-breaches")
def api_sla_breaches():
    """Return current SLA breaches while failing closed on incomplete evidence."""
    now = datetime.now(timezone.utc)
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    tracked_sites = store.list_sites()
    severity_rank = {"critical": 0, "unknown": 1, "warning": 2, "info": 3}
    freshness_rank = {"missing": 0, "invalid": 1, "clock_skew": 2, "stale": 3, "current": 4}
    sites = []
    current_evidence_count = 0
    for site in tracked_sites:
        row = latest_by_url.get(site["url"])
        if row is None:
            freshness = "missing"
            age_hours = None
            latest_snapshot_at = None
            last_observed_breaches = None
            last_observed_score = None
        else:
            freshness, age_hours = _snapshot_freshness(
                row.get("captured_at"),
                now,
                SNAPSHOT_FRESHNESS_HOURS,
            )
            latest_snapshot_at = row.get("captured_at")
            last_observed_breaches = _sla_breaches(row)
            last_observed_breaches.sort(
                key=lambda breach: (severity_rank.get(breach["severity"], 99), breach["target"])
            )
            last_observed_score = row["score"]

        if freshness != "current":
            sites.append(
                {
                    "name": site["name"],
                    "url": site["url"],
                    "client": site.get("client") or "Unassigned",
                    "score": None,
                    "last_observed_score": last_observed_score,
                    "sla_status": "unknown",
                    "breach_count": 0,
                    "highest_severity": "unknown",
                    "snapshot_freshness": freshness,
                    "snapshot_age_hours": age_hours,
                    "latest_snapshot_at": latest_snapshot_at,
                    "breaches": [],
                    "last_observed_breaches": last_observed_breaches,
                    "recommended_action": _sla_evidence_recommended_action(freshness),
                }
            )
            continue

        current_evidence_count += 1
        assert row is not None and last_observed_breaches is not None
        if not last_observed_breaches:
            continue
        highest_severity = last_observed_breaches[0]["severity"]
        sites.append(
            {
                "name": site["name"],
                "url": site["url"],
                "client": site.get("client") or "Unassigned",
                "score": row["score"],
                "last_observed_score": row["score"],
                "sla_status": "breached",
                "breach_count": len(last_observed_breaches),
                "highest_severity": highest_severity,
                "snapshot_freshness": freshness,
                "snapshot_age_hours": age_hours,
                "latest_snapshot_at": latest_snapshot_at,
                "breaches": last_observed_breaches,
                "last_observed_breaches": last_observed_breaches,
                "recommended_action": last_observed_breaches[0]["recommended_action"],
            }
        )

    sites.sort(
        key=lambda site: (
            severity_rank.get(site["highest_severity"], 99),
            freshness_rank.get(site["snapshot_freshness"], 99),
            -site["breach_count"],
            site["score"] if site["score"] is not None else 101,
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    site_count = len(tracked_sites)
    unknown_count = site_count - current_evidence_count
    breach_count = sum(1 for site in sites if site["sla_status"] == "breached")
    critical_breach_count = sum(
        1
        for site in sites
        if site["sla_status"] == "breached" and site["highest_severity"] == "critical"
    )
    warning_breach_count = sum(
        1
        for site in sites
        if site["sla_status"] == "breached" and site["highest_severity"] == "warning"
    )
    status = "red" if critical_breach_count else ("yellow" if warning_breach_count or unknown_count else "green")
    return {
        "generated_at": now.isoformat(),
        "status": status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": site_count,
        "current_evidence_count": current_evidence_count,
        "unknown_count": unknown_count,
        "sla_evidence_percent": round((current_evidence_count / site_count) * 100) if site_count else 100,
        "breach_count": breach_count,
        "critical_breach_count": critical_breach_count,
        "warning_breach_count": warning_breach_count,
        "sites": sites,
    }


def _current_actions(
    snapshot_rows: list[dict] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Build actions only from snapshots with current monitoring evidence."""
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    actions = []
    rows = snapshot_rows if snapshot_rows is not None else store.latest_dashboard()
    for row in _current_snapshot_rows(rows, now):
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
    """Return current fleet alerts without hiding incomplete monitoring evidence."""
    now = datetime.now(timezone.utc)
    dashboard_rows = store.latest_dashboard()
    tracked_sites = store.list_sites()
    current_snapshot_count = len(_current_snapshot_rows(dashboard_rows, now))
    stale_snapshot_count = len(dashboard_rows) - current_snapshot_count
    monitored_urls = {row["url"] for row in dashboard_rows}
    missing_snapshot_count = sum(1 for site in tracked_sites if site["url"] not in monitored_urls)
    monitoring_gap_count = stale_snapshot_count + missing_snapshot_count
    actions = _current_actions(dashboard_rows, now)
    critical_action_count = sum(1 for action in actions if action["severity"] == "critical")
    status = "red" if critical_action_count else ("yellow" if actions or monitoring_gap_count else "green")
    return {
        "generated_at": now.isoformat(),
        "status": status,
        "tracked_site_count": len(tracked_sites),
        "current_snapshot_count": current_snapshot_count,
        "stale_snapshot_count": stale_snapshot_count,
        "missing_snapshot_count": missing_snapshot_count,
        "monitoring_gap_count": monitoring_gap_count,
        "action_count": len(actions),
        "actions": actions,
    }


def _site_watchlist_rows() -> list[dict]:
    """Return current snapshot rows that need operator attention."""
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    sites = []
    for row in _current_snapshot_rows(store.latest_dashboard()):
        alerts = row["alerts"]
        if row["score"] >= 85 and not alerts:
            continue
        top_alert = min(alerts, key=lambda alert: severity_rank.get(alert.get("severity", "info"), 99)) if alerts else None
        watch_status = "critical" if _dashboard_status(row["score"]) == "red" else "warning"
        if top_alert and top_alert.get("severity") == "critical":
            watch_status = "critical"
        sites.append(
            {
                "name": row["name"],
                "url": row["url"],
                "client": row.get("client") or "Unassigned",
                "score": row["score"],
                "watch_status": watch_status,
                "alert_count": len(alerts),
                "top_alert": top_alert.get("message") if top_alert else "Score is below target.",
                "top_alert_severity": top_alert.get("severity") if top_alert else watch_status,
                "recommended_action": _recommended_action(top_alert or {}),
                "latest_snapshot_at": row["captured_at"],
            }
        )

    sites.sort(
        key=lambda site: (
            severity_rank.get(site["watch_status"], 99),
            site["score"],
            -site["alert_count"],
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    return sites


@app.get("/api/site-watchlist")
def api_site_watchlist():
    """Return current attention sites plus fail-closed monitoring-gap counts."""
    now = datetime.now(timezone.utc)
    dashboard_rows = store.latest_dashboard()
    tracked_sites = store.list_sites()
    sites = _site_watchlist_rows()
    current_snapshot_count = len(_current_snapshot_rows(dashboard_rows, now))
    stale_snapshot_count = len(dashboard_rows) - current_snapshot_count
    monitored_urls = {row["url"] for row in dashboard_rows}
    missing_snapshot_count = sum(1 for site in tracked_sites if site["url"] not in monitored_urls)
    monitoring_gap_count = stale_snapshot_count + missing_snapshot_count
    critical_watch_count = sum(1 for site in sites if site["watch_status"] == "critical")
    status = "red" if critical_watch_count else ("yellow" if sites or monitoring_gap_count else "green")
    return {
        "generated_at": now.isoformat(),
        "status": status,
        "site_count": len(dashboard_rows),
        "tracked_site_count": len(tracked_sites),
        "current_snapshot_count": current_snapshot_count,
        "stale_snapshot_count": stale_snapshot_count,
        "missing_snapshot_count": missing_snapshot_count,
        "monitoring_gap_count": monitoring_gap_count,
        "watchlist_count": len(sites),
        "critical_watch_count": critical_watch_count,
        "sites": sites,
    }


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


def _availability_recommended_action(status: str, freshness: str) -> str:
    """Return the safest next step for current or incomplete availability evidence."""
    if status == "down":
        return "Confirm site availability, hosting status, DNS, and recent deploys."
    if freshness == "missing":
        return "Capture an initial fleet snapshot and verify site availability."
    if freshness == "clock_skew":
        return "Correct the snapshot timestamp or source clock, then verify site availability."
    if freshness == "invalid":
        return "Repair the invalid snapshot timestamp, then verify site availability."
    if freshness == "stale":
        return "Capture a fresh fleet snapshot before relying on availability status."
    return "Continue normal availability monitoring."


@app.get("/api/availability")
def api_availability():
    """Return availability status while failing closed on missing or stale evidence."""
    now = datetime.now(timezone.utc)
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    sites = []
    for site in store.list_sites():
        row = latest_by_url.get(site["url"])
        if row is None:
            freshness = "missing"
            age_hours = None
            latest_snapshot_at = None
            last_observed_reachable = None
            availability_status = "unknown"
            reachable = None
        else:
            freshness, age_hours = _snapshot_freshness(
                row.get("captured_at"),
                now,
                SNAPSHOT_FRESHNESS_HOURS,
            )
            latest_snapshot_at = row.get("captured_at")
            last_observed_reachable = bool(row["uptime_ok"])
            if freshness != "current":
                availability_status = "unknown"
                reachable = None
            else:
                reachable = last_observed_reachable
                availability_status = "available" if reachable else "down"

        sites.append(
            {
                "name": site["name"],
                "url": site["url"],
                "client": site.get("client") or "Unassigned",
                "availability_status": availability_status,
                "snapshot_freshness": freshness,
                "snapshot_age_hours": age_hours,
                "reachable": reachable,
                "last_observed_reachable": last_observed_reachable,
                "latest_snapshot_at": latest_snapshot_at,
                "recommended_action": _availability_recommended_action(availability_status, freshness),
            }
        )

    status_rank = {"down": 0, "unknown": 1, "available": 2}
    freshness_rank = {"missing": 0, "invalid": 1, "clock_skew": 2, "stale": 3, "current": 4}
    sites.sort(
        key=lambda site: (
            status_rank.get(site["availability_status"], 99),
            freshness_rank.get(site["snapshot_freshness"], 99),
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    current_evidence_count = sum(1 for site in sites if site["snapshot_freshness"] == "current")
    available_count = sum(1 for site in sites if site["availability_status"] == "available")
    down_count = sum(1 for site in sites if site["availability_status"] == "down")
    unknown_count = len(sites) - current_evidence_count
    status = "red" if down_count else ("yellow" if unknown_count else "green")
    return {
        "generated_at": now.isoformat(),
        "status": status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": len(sites),
        "current_evidence_count": current_evidence_count,
        "available_count": available_count,
        "down_count": down_count,
        "unknown_count": unknown_count,
        "availability_evidence_percent": round((current_evidence_count / len(sites)) * 100) if sites else 100,
        "sites": sites,
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


def _backup_evidence_recommended_action(status: str, freshness: str) -> str:
    """Return a backup next step without trusting incomplete snapshot evidence."""
    if freshness == "missing":
        return "Capture an initial fleet snapshot and verify backup freshness."
    if freshness == "clock_skew":
        return "Correct the snapshot timestamp or source clock, then verify backup freshness."
    if freshness == "invalid":
        return "Repair the invalid snapshot timestamp, then verify backup freshness."
    if freshness == "stale":
        return "Capture a fresh fleet snapshot before relying on backup status."
    return _backup_recommended_action(status)


@app.get("/api/backups")
def api_backups():
    """Return backup status while failing closed on missing or stale evidence."""
    now = datetime.now(timezone.utc)
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    sites = []
    for site in store.list_sites():
        row = latest_by_url.get(site["url"])
        if row is None:
            freshness = "missing"
            age_hours = None
            latest_snapshot_at = None
            last_observed_backup_age_hours = None
            backup_age_hours = None
            status = "unknown"
        else:
            freshness, age_hours = _snapshot_freshness(
                row.get("captured_at"),
                now,
                SNAPSHOT_FRESHNESS_HOURS,
            )
            latest_snapshot_at = row.get("captured_at")
            last_observed_backup_age_hours = row["backup_age_hours"]
            if freshness == "current":
                backup_age_hours = last_observed_backup_age_hours
                status = _backup_status(backup_age_hours)
            else:
                backup_age_hours = None
                status = "unknown"

        sites.append(
            {
                "name": site["name"],
                "url": site["url"],
                "client": site.get("client") or "Unassigned",
                "backup_age_hours": backup_age_hours,
                "last_observed_backup_age_hours": last_observed_backup_age_hours,
                "backup_status": status,
                "snapshot_freshness": freshness,
                "snapshot_age_hours": age_hours,
                "latest_snapshot_at": latest_snapshot_at,
                "recommended_action": _backup_evidence_recommended_action(status, freshness),
            }
        )

    status_rank = {"critical": 0, "unknown": 1, "warning": 2, "fresh": 3}
    freshness_rank = {"missing": 0, "invalid": 1, "clock_skew": 2, "stale": 3, "current": 4}
    sites.sort(
        key=lambda site: (
            status_rank.get(site["backup_status"], 99),
            freshness_rank.get(site["snapshot_freshness"], 99),
            -(site["last_observed_backup_age_hours"] or 0),
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    current_evidence_count = sum(1 for site in sites if site["snapshot_freshness"] == "current")
    fresh_count = sum(1 for site in sites if site["backup_status"] == "fresh")
    warning_count = sum(1 for site in sites if site["backup_status"] == "warning")
    critical_count = sum(1 for site in sites if site["backup_status"] == "critical")
    unknown_count = len(sites) - current_evidence_count
    overall_status = "red" if critical_count else ("yellow" if warning_count or unknown_count else "green")
    current_backup_ages = [
        site["backup_age_hours"]
        for site in sites
        if site["backup_age_hours"] is not None
    ]
    return {
        "generated_at": now.isoformat(),
        "status": overall_status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": len(sites),
        "current_evidence_count": current_evidence_count,
        "fresh_count": fresh_count,
        "warning_count": warning_count,
        "critical_count": critical_count,
        "stale_count": warning_count + critical_count,
        "unknown_count": unknown_count,
        "backup_evidence_percent": round((current_evidence_count / len(sites)) * 100) if sites else 100,
        "oldest_backup_age_hours": max(current_backup_ages, default=0),
        "sites": sites,
    }


def _backup_remediation_recommended_action(
    critical_count: int,
    warning_count: int,
    unknown_count: int = 0,
) -> str:
    if critical_count and warning_count and unknown_count:
        return "Run immediate backups for critical sites, refresh incomplete evidence, then verify warning-site schedules."
    if critical_count and warning_count:
        return "Run immediate backups for critical sites, then verify schedules for warning sites."
    if critical_count and unknown_count:
        return "Run immediate backups for critical sites, then refresh missing or stale backup evidence."
    if critical_count:
        return "Run immediate backups for every critical site and verify restore points."
    if warning_count and unknown_count:
        return "Refresh missing or stale backup evidence, then confirm scheduled backups for warning sites."
    if warning_count:
        return "Confirm the next scheduled backup completes successfully for warning sites."
    if unknown_count:
        return "Capture fresh fleet snapshots before scheduling backup remediation."
    return "Continue normal backup monitoring."


@app.get("/api/backup-remediation")
def api_backup_remediation():
    """Group current backup work by client while failing closed on incomplete evidence."""
    now = datetime.now(timezone.utc)
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    tracked_sites = store.list_sites()
    client_rows: dict[str, dict] = {}
    for site in tracked_sites:
        row = latest_by_url.get(site["url"])
        client_name = site.get("client") or "Unassigned"
        summary = client_rows.setdefault(
            client_name,
            {
                "client": client_name,
                "site_count": 0,
                "current_evidence_count": 0,
                "unknown_site_count": 0,
                "fresh_site_count": 0,
                "warning_site_count": 0,
                "critical_site_count": 0,
                "stale_site_count": 0,
                "oldest_backup_age_hours": 0,
                "backup_status": "fresh",
                "sites": [],
            },
        )
        summary["site_count"] += 1

        if row is None:
            freshness = "missing"
            snapshot_age_hours = None
            latest_snapshot_at = None
            last_observed_backup_age_hours = None
        else:
            freshness, snapshot_age_hours = _snapshot_freshness(
                row.get("captured_at"),
                now,
                SNAPSHOT_FRESHNESS_HOURS,
            )
            latest_snapshot_at = row.get("captured_at")
            last_observed_backup_age_hours = row["backup_age_hours"]

        if freshness != "current":
            summary["unknown_site_count"] += 1
            if summary["backup_status"] != "critical":
                summary["backup_status"] = "unknown"
            summary["sites"].append(
                {
                    "name": site["name"],
                    "url": site["url"],
                    "backup_age_hours": None,
                    "last_observed_backup_age_hours": last_observed_backup_age_hours,
                    "backup_status": "unknown",
                    "snapshot_freshness": freshness,
                    "snapshot_age_hours": snapshot_age_hours,
                    "latest_snapshot_at": latest_snapshot_at,
                    "recommended_action": _backup_evidence_recommended_action("unknown", freshness),
                }
            )
            continue

        assert row is not None and last_observed_backup_age_hours is not None
        summary["current_evidence_count"] += 1
        status = _backup_status(last_observed_backup_age_hours)
        summary["oldest_backup_age_hours"] = max(
            summary["oldest_backup_age_hours"],
            last_observed_backup_age_hours,
        )
        if status == "critical":
            summary["critical_site_count"] += 1
            summary["stale_site_count"] += 1
            summary["backup_status"] = "critical"
        elif status == "warning":
            summary["warning_site_count"] += 1
            summary["stale_site_count"] += 1
            if summary["backup_status"] == "fresh":
                summary["backup_status"] = "warning"
        else:
            summary["fresh_site_count"] += 1

        if status != "fresh":
            summary["sites"].append(
                {
                    "name": site["name"],
                    "url": site["url"],
                    "backup_age_hours": last_observed_backup_age_hours,
                    "last_observed_backup_age_hours": last_observed_backup_age_hours,
                    "backup_status": status,
                    "snapshot_freshness": freshness,
                    "snapshot_age_hours": snapshot_age_hours,
                    "latest_snapshot_at": latest_snapshot_at,
                    "recommended_action": _backup_recommended_action(status),
                }
            )

    status_rank = {"critical": 0, "unknown": 1, "warning": 2, "fresh": 3}
    freshness_rank = {"missing": 0, "invalid": 1, "clock_skew": 2, "stale": 3, "current": 4}
    clients = []
    for summary in client_rows.values():
        summary["sites"].sort(
            key=lambda site: (
                status_rank.get(site["backup_status"], 99),
                freshness_rank.get(site["snapshot_freshness"], 99),
                -(site["last_observed_backup_age_hours"] or 0),
                site["name"].lower(),
            )
        )
        summary["backup_evidence_percent"] = round(
            (summary["current_evidence_count"] / summary["site_count"]) * 100
        ) if summary["site_count"] else 100
        summary["recommended_action"] = _backup_remediation_recommended_action(
            summary["critical_site_count"],
            summary["warning_site_count"],
            summary["unknown_site_count"],
        )
        clients.append(summary)

    clients.sort(
        key=lambda row: (
            status_rank.get(row["backup_status"], 99),
            -row["critical_site_count"],
            -row["unknown_site_count"],
            -row["warning_site_count"],
            -row["stale_site_count"],
            -row["oldest_backup_age_hours"],
            row["client"].lower(),
        )
    )
    site_count = len(tracked_sites)
    current_evidence_count = sum(row["current_evidence_count"] for row in clients)
    unknown_count = site_count - current_evidence_count
    stale_site_count = sum(row["stale_site_count"] for row in clients)
    critical_site_count = sum(row["critical_site_count"] for row in clients)
    warning_site_count = sum(row["warning_site_count"] for row in clients)
    status = "red" if critical_site_count else ("yellow" if warning_site_count or unknown_count else "green")
    return {
        "generated_at": now.isoformat(),
        "status": status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "client_count": len(clients),
        "site_count": site_count,
        "current_evidence_count": current_evidence_count,
        "unknown_count": unknown_count,
        "backup_evidence_percent": round((current_evidence_count / site_count) * 100) if site_count else 100,
        "stale_site_count": stale_site_count,
        "critical_site_count": critical_site_count,
        "clients": clients,
    }


def _restore_drill_priority(backup_age_hours: int) -> str:
    """Return restore-drill priority based on backup freshness."""
    if backup_age_hours > 168:
        return "urgent"
    if backup_age_hours > 72:
        return "high"
    if backup_age_hours > 24:
        return "watch"
    return "routine"


def _restore_drill_recommended_action(priority: str) -> str:
    if priority == "urgent":
        return "Run an immediate restore drill and verify a recent usable backup exists."
    if priority == "high":
        return "Schedule a restore drill after creating and validating a fresh backup."
    if priority == "watch":
        return "Confirm the next scheduled backup and include the site in the next drill rotation."
    return "Keep the site in the normal quarterly restore-drill rotation."


def _restore_drill_evidence_recommended_action(priority: str, freshness: str) -> str:
    """Return a restore-drill next step without trusting incomplete evidence."""
    if freshness == "missing":
        return "Capture an initial fleet snapshot and verify backup restore readiness."
    if freshness == "clock_skew":
        return "Correct the snapshot timestamp or source clock, then verify backup restore readiness."
    if freshness == "invalid":
        return "Repair the invalid snapshot timestamp, then verify backup restore readiness."
    if freshness == "stale":
        return "Capture a fresh fleet snapshot before scheduling a restore drill."
    return _restore_drill_recommended_action(priority)


@app.get("/api/restore-drill-queue")
def api_restore_drill_queue():
    """Return restore-drill priorities while failing closed on incomplete evidence."""
    now = datetime.now(timezone.utc)
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    priority_rank = {"unknown": 0, "urgent": 1, "high": 2, "watch": 3, "routine": 4}
    freshness_rank = {"missing": 0, "invalid": 1, "clock_skew": 2, "stale": 3, "current": 4}
    sites = []
    for site in store.list_sites():
        row = latest_by_url.get(site["url"])
        if row is None:
            freshness = "missing"
            age_hours = None
            latest_snapshot_at = None
            last_observed_backup_age_hours = None
            backup_age_hours = None
            priority = "unknown"
        else:
            freshness, age_hours = _snapshot_freshness(
                row.get("captured_at"),
                now,
                SNAPSHOT_FRESHNESS_HOURS,
            )
            latest_snapshot_at = row.get("captured_at")
            last_observed_backup_age_hours = row["backup_age_hours"]
            if freshness == "current":
                backup_age_hours = last_observed_backup_age_hours
                priority = _restore_drill_priority(backup_age_hours)
            else:
                backup_age_hours = None
                priority = "unknown"
        sites.append(
            {
                "name": site["name"],
                "url": site["url"],
                "client": site.get("client") or "Unassigned",
                "backup_age_hours": backup_age_hours,
                "last_observed_backup_age_hours": last_observed_backup_age_hours,
                "restore_drill_priority": priority,
                "snapshot_freshness": freshness,
                "snapshot_age_hours": age_hours,
                "latest_snapshot_at": latest_snapshot_at,
                "recommended_action": _restore_drill_evidence_recommended_action(priority, freshness),
            }
        )

    sites.sort(
        key=lambda site: (
            priority_rank.get(site["restore_drill_priority"], 99),
            freshness_rank.get(site["snapshot_freshness"], 99),
            -(site["last_observed_backup_age_hours"] or 0),
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    current_evidence_count = sum(1 for site in sites if site["snapshot_freshness"] == "current")
    urgent_count = sum(1 for site in sites if site["restore_drill_priority"] == "urgent")
    high_count = sum(1 for site in sites if site["restore_drill_priority"] == "high")
    watch_count = sum(1 for site in sites if site["restore_drill_priority"] == "watch")
    routine_count = sum(1 for site in sites if site["restore_drill_priority"] == "routine")
    unknown_count = len(sites) - current_evidence_count
    status = "red" if urgent_count else ("yellow" if unknown_count or high_count or watch_count else "green")
    return {
        "generated_at": now.isoformat(),
        "status": status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": len(sites),
        "current_evidence_count": current_evidence_count,
        "unknown_count": unknown_count,
        "urgent_count": urgent_count,
        "high_count": high_count,
        "watch_count": watch_count,
        "routine_count": routine_count,
        "restore_evidence_percent": round((current_evidence_count / len(sites)) * 100) if sites else 100,
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


def _security_evidence_recommended_action(status: str, freshness: str) -> str:
    """Return a security next step without trusting incomplete snapshot evidence."""
    if freshness == "missing":
        return "Capture an initial fleet snapshot and verify security header coverage."
    if freshness == "clock_skew":
        return "Correct the snapshot timestamp or source clock, then verify security header coverage."
    if freshness == "invalid":
        return "Repair the invalid snapshot timestamp, then verify security header coverage."
    if freshness == "stale":
        return "Capture a fresh fleet snapshot before relying on security coverage."
    return _security_recommended_action(status)


@app.get("/api/security")
def api_security():
    """Return security status while failing closed on missing or stale evidence."""
    now = datetime.now(timezone.utc)
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    sites = []
    for site in store.list_sites():
        row = latest_by_url.get(site["url"])
        if row is None:
            freshness = "missing"
            age_hours = None
            latest_snapshot_at = None
            last_observed_security_header_count = None
            security_header_count = None
            status = "unknown"
        else:
            freshness, age_hours = _snapshot_freshness(
                row.get("captured_at"),
                now,
                SNAPSHOT_FRESHNESS_HOURS,
            )
            latest_snapshot_at = row.get("captured_at")
            last_observed_security_header_count = row["security_header_count"]
            if freshness == "current":
                security_header_count = last_observed_security_header_count
                status = _security_status(security_header_count)
            else:
                security_header_count = None
                status = "unknown"

        sites.append(
            {
                "name": site["name"],
                "url": site["url"],
                "client": site.get("client") or "Unassigned",
                "security_header_count": security_header_count,
                "last_observed_security_header_count": last_observed_security_header_count,
                "security_status": status,
                "snapshot_freshness": freshness,
                "snapshot_age_hours": age_hours,
                "latest_snapshot_at": latest_snapshot_at,
                "recommended_action": _security_evidence_recommended_action(status, freshness),
            }
        )

    status_rank = {"critical": 0, "unknown": 1, "warning": 2, "covered": 3}
    freshness_rank = {"missing": 0, "invalid": 1, "clock_skew": 2, "stale": 3, "current": 4}
    sites.sort(
        key=lambda site: (
            status_rank.get(site["security_status"], 99),
            freshness_rank.get(site["snapshot_freshness"], 99),
            site["security_header_count"] if site["security_header_count"] is not None else -1,
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    current_evidence_count = sum(1 for site in sites if site["snapshot_freshness"] == "current")
    covered_count = sum(1 for site in sites if site["security_status"] == "covered")
    warning_count = sum(1 for site in sites if site["security_status"] == "warning")
    critical_count = sum(1 for site in sites if site["security_status"] == "critical")
    unknown_count = len(sites) - current_evidence_count
    overall_status = "red" if critical_count else ("yellow" if warning_count or unknown_count else "green")
    current_header_counts = [
        site["security_header_count"]
        for site in sites
        if site["security_header_count"] is not None
    ]
    return {
        "generated_at": now.isoformat(),
        "status": overall_status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": len(sites),
        "current_evidence_count": current_evidence_count,
        "covered_count": covered_count,
        "warning_count": warning_count,
        "critical_count": critical_count,
        "gap_count": warning_count + critical_count,
        "unknown_count": unknown_count,
        "security_evidence_percent": round((current_evidence_count / len(sites)) * 100) if sites else 100,
        "average_security_header_count": round(
            sum(current_header_counts) / len(current_header_counts), 1
        )
        if current_header_counts
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


def _performance_evidence_recommended_action(status: str, freshness: str) -> str:
    """Return a performance next step without trusting incomplete evidence."""
    if freshness == "missing":
        return "Capture an initial fleet snapshot and verify response time."
    if freshness == "clock_skew":
        return "Correct the snapshot timestamp or source clock, then verify response time."
    if freshness == "invalid":
        return "Repair the invalid snapshot timestamp, then verify response time."
    if freshness == "stale":
        return "Capture a fresh fleet snapshot before relying on performance status."
    return _performance_recommended_action(status)


@app.get("/api/performance")
def api_performance():
    """Return performance status while failing closed on incomplete evidence."""
    now = datetime.now(timezone.utc)
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    sites = []
    for site in store.list_sites():
        row = latest_by_url.get(site["url"])
        if row is None:
            freshness = "missing"
            age_hours = None
            latest_snapshot_at = None
            last_observed_response_ms = None
            response_ms = None
            status = "unknown"
        else:
            freshness, age_hours = _snapshot_freshness(
                row.get("captured_at"),
                now,
                SNAPSHOT_FRESHNESS_HOURS,
            )
            latest_snapshot_at = row.get("captured_at")
            last_observed_response_ms = row["response_ms"]
            if freshness == "current":
                response_ms = last_observed_response_ms
                status = _performance_status(response_ms)
            else:
                response_ms = None
                status = "unknown"

        sites.append(
            {
                "name": site["name"],
                "url": site["url"],
                "client": site.get("client") or "Unassigned",
                "response_ms": response_ms,
                "last_observed_response_ms": last_observed_response_ms,
                "performance_status": status,
                "snapshot_freshness": freshness,
                "snapshot_age_hours": age_hours,
                "latest_snapshot_at": latest_snapshot_at,
                "recommended_action": _performance_evidence_recommended_action(status, freshness),
            }
        )

    status_rank = {"slow": 0, "unknown": 1, "warning": 2, "fast": 3}
    freshness_rank = {"missing": 0, "invalid": 1, "clock_skew": 2, "stale": 3, "current": 4}
    sites.sort(
        key=lambda site: (
            status_rank.get(site["performance_status"], 99),
            freshness_rank.get(site["snapshot_freshness"], 99),
            -(site["last_observed_response_ms"] or 0),
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    current_evidence_count = sum(1 for site in sites if site["snapshot_freshness"] == "current")
    slow_count = sum(1 for site in sites if site["performance_status"] == "slow")
    warning_count = sum(1 for site in sites if site["performance_status"] == "warning")
    fast_count = sum(1 for site in sites if site["performance_status"] == "fast")
    unknown_count = len(sites) - current_evidence_count
    overall_status = "red" if slow_count else ("yellow" if warning_count or unknown_count else "green")
    current_response_times = [
        site["response_ms"]
        for site in sites
        if site["response_ms"] is not None
    ]
    return {
        "generated_at": now.isoformat(),
        "status": overall_status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": len(sites),
        "current_evidence_count": current_evidence_count,
        "slow_count": slow_count,
        "warning_count": warning_count,
        "fast_count": fast_count,
        "unknown_count": unknown_count,
        "performance_evidence_percent": round((current_evidence_count / len(sites)) * 100) if sites else 100,
        "average_response_ms": round(sum(current_response_times) / len(current_response_times))
        if current_response_times
        else 0,
        "max_response_ms": max(current_response_times, default=0),
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


def _certificate_evidence_recommended_action(status: str, freshness: str) -> str:
    """Return a certificate next step without trusting incomplete snapshot evidence."""
    if freshness == "missing":
        return "Capture an initial fleet snapshot and verify certificate expiry."
    if freshness == "clock_skew":
        return "Correct the snapshot timestamp or source clock, then verify certificate expiry."
    if freshness == "invalid":
        return "Repair the invalid snapshot timestamp, then verify certificate expiry."
    if freshness == "stale":
        return "Capture a fresh fleet snapshot before relying on certificate status."
    return _certificate_recommended_action(status)


@app.get("/api/certificates")
def api_certificates():
    """Return certificate status while failing closed on missing or stale evidence."""
    now = datetime.now(timezone.utc)
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    sites = []
    for site in store.list_sites():
        row = latest_by_url.get(site["url"])
        if row is None:
            freshness = "missing"
            age_hours = None
            latest_snapshot_at = None
            last_observed_ssl_days = None
            ssl_days = None
            status = "unknown"
        else:
            freshness, age_hours = _snapshot_freshness(
                row.get("captured_at"),
                now,
                SNAPSHOT_FRESHNESS_HOURS,
            )
            latest_snapshot_at = row.get("captured_at")
            last_observed_ssl_days = row["ssl_days"]
            if freshness == "current":
                ssl_days = last_observed_ssl_days
                status = _certificate_status(ssl_days)
            else:
                ssl_days = None
                status = "unknown"

        sites.append(
            {
                "name": site["name"],
                "url": site["url"],
                "client": site.get("client") or "Unassigned",
                "ssl_days_remaining": ssl_days,
                "last_observed_ssl_days_remaining": last_observed_ssl_days,
                "certificate_status": status,
                "snapshot_freshness": freshness,
                "snapshot_age_hours": age_hours,
                "latest_snapshot_at": latest_snapshot_at,
                "recommended_action": _certificate_evidence_recommended_action(status, freshness),
            }
        )

    status_rank = {"critical": 0, "unknown": 1, "warning": 2, "healthy": 3}
    freshness_rank = {"missing": 0, "invalid": 1, "clock_skew": 2, "stale": 3, "current": 4}
    sites.sort(
        key=lambda site: (
            status_rank.get(site["certificate_status"], 99),
            freshness_rank.get(site["snapshot_freshness"], 99),
            site["last_observed_ssl_days_remaining"]
            if site["last_observed_ssl_days_remaining"] is not None
            else float("inf"),
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    current_evidence_count = sum(1 for site in sites if site["snapshot_freshness"] == "current")
    critical_count = sum(1 for site in sites if site["certificate_status"] == "critical")
    warning_count = sum(1 for site in sites if site["certificate_status"] == "warning")
    healthy_count = sum(1 for site in sites if site["certificate_status"] == "healthy")
    unknown_count = len(sites) - current_evidence_count
    overall_status = "red" if critical_count else ("yellow" if warning_count or unknown_count else "green")
    current_ssl_days = [
        site["ssl_days_remaining"]
        for site in sites
        if site["ssl_days_remaining"] is not None
    ]
    return {
        "generated_at": now.isoformat(),
        "status": overall_status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": len(sites),
        "current_evidence_count": current_evidence_count,
        "healthy_count": healthy_count,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "unknown_count": unknown_count,
        "certificate_evidence_percent": round((current_evidence_count / len(sites)) * 100) if sites else 100,
        "minimum_ssl_days": min(current_ssl_days, default=None),
        "sites": sites,
    }


def _certificate_renewal_window(ssl_days: int) -> str:
    """Map a certificate expiry horizon into an operator renewal window."""
    if ssl_days <= 0:
        return "overdue"
    if ssl_days <= 7:
        return "immediate"
    if ssl_days <= 30:
        return "scheduled"
    return "healthy"


def _certificate_renewal_action(window: str) -> str:
    if window == "overdue":
        return "Replace the expired certificate and verify HTTPS immediately."
    if window == "immediate":
        return "Renew the certificate this week and confirm post-renewal expiry."
    if window == "scheduled":
        return "Schedule certificate renewal before the 7-day critical window."
    return "No renewal action is needed in the next 30 days."


def _certificate_renewal_evidence_action(window: str, freshness: str) -> str:
    """Return a calendar action without treating incomplete evidence as current."""
    if freshness != "current":
        return _certificate_evidence_recommended_action("unknown", freshness)
    return _certificate_renewal_action(window)


@app.get("/api/certificate-renewal-calendar")
def api_certificate_renewal_calendar():
    """Return current TLS renewals while failing closed on incomplete evidence."""
    now = datetime.now(timezone.utc)
    tracked_sites = store.list_sites()
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    window_rank = {"overdue": 0, "immediate": 1, "unknown": 2, "scheduled": 3}
    freshness_rank = {"missing": 0, "invalid": 1, "clock_skew": 2, "stale": 3, "current": 4}
    sites = []
    current_evidence_count = 0
    for site in tracked_sites:
        row = latest_by_url.get(site["url"])
        if row is None:
            freshness = "missing"
            age_hours = None
            latest_snapshot_at = None
            last_observed_ssl_days = None
            ssl_days = None
            window = "unknown"
        else:
            freshness, age_hours = _snapshot_freshness(
                row.get("captured_at"),
                now,
                SNAPSHOT_FRESHNESS_HOURS,
            )
            latest_snapshot_at = row.get("captured_at")
            last_observed_ssl_days = row["ssl_days"]
            if freshness == "current":
                current_evidence_count += 1
                ssl_days = last_observed_ssl_days
                window = _certificate_renewal_window(ssl_days)
            else:
                ssl_days = None
                window = "unknown"

        if window == "healthy":
            continue
        sites.append(
            {
                "name": site["name"],
                "url": site["url"],
                "client": site.get("client") or "Unassigned",
                "ssl_days_remaining": ssl_days,
                "last_observed_ssl_days_remaining": last_observed_ssl_days,
                "renewal_window": window,
                "snapshot_freshness": freshness,
                "snapshot_age_hours": age_hours,
                "latest_snapshot_at": latest_snapshot_at,
                "recommended_action": _certificate_renewal_evidence_action(window, freshness),
            }
        )

    sites.sort(
        key=lambda site: (
            window_rank[site["renewal_window"]],
            freshness_rank.get(site["snapshot_freshness"], 99),
            site["last_observed_ssl_days_remaining"]
            if site["last_observed_ssl_days_remaining"] is not None
            else float("inf"),
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    overdue_count = sum(1 for site in sites if site["renewal_window"] == "overdue")
    immediate_count = sum(1 for site in sites if site["renewal_window"] == "immediate")
    scheduled_count = sum(1 for site in sites if site["renewal_window"] == "scheduled")
    unknown_count = len(tracked_sites) - current_evidence_count
    overall_status = "red" if overdue_count or immediate_count else (
        "yellow" if scheduled_count or unknown_count else "green"
    )
    return {
        "generated_at": now.isoformat(),
        "status": overall_status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": len(tracked_sites),
        "current_evidence_count": current_evidence_count,
        "renewal_count": overdue_count + immediate_count + scheduled_count,
        "overdue_count": overdue_count,
        "immediate_count": immediate_count,
        "scheduled_count": scheduled_count,
        "unknown_count": unknown_count,
        "renewal_evidence_percent": round((current_evidence_count / len(tracked_sites)) * 100)
        if tracked_sites
        else 100,
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


def _update_evidence_recommended_action(status: str, freshness: str) -> str:
    """Return an update next step without trusting incomplete snapshot evidence."""
    if freshness == "missing":
        return "Capture an initial fleet snapshot and verify the WordPress update backlog."
    if freshness == "clock_skew":
        return "Correct the snapshot timestamp or source clock, then verify the WordPress update backlog."
    if freshness == "invalid":
        return "Repair the invalid snapshot timestamp, then verify the WordPress update backlog."
    if freshness == "stale":
        return "Capture a fresh fleet snapshot before relying on update status."
    return _update_recommended_action(status)


@app.get("/api/updates")
def api_updates():
    """Return update status while failing closed on missing or stale evidence."""
    now = datetime.now(timezone.utc)
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    sites = []
    for site in store.list_sites():
        row = latest_by_url.get(site["url"])
        if row is None:
            freshness = "missing"
            age_hours = None
            latest_snapshot_at = None
            last_observed_pending_updates = None
            pending_updates = None
            status = "unknown"
        else:
            freshness, age_hours = _snapshot_freshness(
                row.get("captured_at"),
                now,
                SNAPSHOT_FRESHNESS_HOURS,
            )
            latest_snapshot_at = row.get("captured_at")
            last_observed_pending_updates = row["wp_updates"]
            if freshness == "current":
                pending_updates = last_observed_pending_updates
                status = _update_status(pending_updates)
            else:
                pending_updates = None
                status = "unknown"

        sites.append(
            {
                "name": site["name"],
                "url": site["url"],
                "client": site.get("client") or "Unassigned",
                "pending_updates": pending_updates,
                "last_observed_pending_updates": last_observed_pending_updates,
                "update_status": status,
                "snapshot_freshness": freshness,
                "snapshot_age_hours": age_hours,
                "latest_snapshot_at": latest_snapshot_at,
                "recommended_action": _update_evidence_recommended_action(status, freshness),
            }
        )

    status_rank = {"critical": 0, "unknown": 1, "warning": 2, "current": 3}
    freshness_rank = {"missing": 0, "invalid": 1, "clock_skew": 2, "stale": 3, "current": 4}
    sites.sort(
        key=lambda site: (
            status_rank.get(site["update_status"], 99),
            freshness_rank.get(site["snapshot_freshness"], 99),
            -(site["last_observed_pending_updates"] or 0),
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    current_evidence_count = sum(1 for site in sites if site["snapshot_freshness"] == "current")
    critical_count = sum(1 for site in sites if site["update_status"] == "critical")
    warning_count = sum(1 for site in sites if site["update_status"] == "warning")
    current_count = sum(1 for site in sites if site["update_status"] == "current")
    unknown_count = len(sites) - current_evidence_count
    overall_status = "red" if critical_count else ("yellow" if warning_count or unknown_count else "green")
    current_update_counts = [
        site["pending_updates"]
        for site in sites
        if site["pending_updates"] is not None
    ]
    return {
        "generated_at": now.isoformat(),
        "status": overall_status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": len(sites),
        "current_evidence_count": current_evidence_count,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "current_count": current_count,
        "unknown_count": unknown_count,
        "update_evidence_percent": round((current_evidence_count / len(sites)) * 100) if sites else 100,
        "backlog_count": critical_count + warning_count,
        "total_pending_updates": sum(current_update_counts),
        "max_pending_updates": max(current_update_counts, default=0),
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


def _risk_register_entries(rows: list[dict]) -> list[dict]:
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
        for row in rows:
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
    """Return current operational risks while surfacing incomplete evidence."""
    now = datetime.now(timezone.utc)
    tracked_site_count = len(store.list_sites())
    dashboard_rows = store.latest_dashboard()
    current_rows = _current_snapshot_rows(dashboard_rows, now)
    current_evidence_count = len(current_rows)
    monitored_site_count = len(dashboard_rows)
    missing_snapshot_count = tracked_site_count - monitored_site_count
    stale_snapshot_count = monitored_site_count - current_evidence_count
    unknown_count = tracked_site_count - current_evidence_count
    entries = _risk_register_entries(current_rows)
    critical_category_count = sum(
        1 for entry in entries if entry["highest_severity"] == "critical"
    )
    warning_category_count = sum(
        1 for entry in entries if entry["highest_severity"] == "warning"
    )
    status = "red" if critical_category_count else (
        "yellow" if warning_category_count or unknown_count else "green"
    )
    return {
        "generated_at": now.isoformat(),
        "status": status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": tracked_site_count,
        "monitored_site_count": monitored_site_count,
        "current_evidence_count": current_evidence_count,
        "missing_snapshot_count": missing_snapshot_count,
        "stale_snapshot_count": stale_snapshot_count,
        "unknown_count": unknown_count,
        "risk_evidence_percent": (
            round((current_evidence_count / tracked_site_count) * 100)
            if tracked_site_count
            else 100
        ),
        "category_count": len(entries),
        "critical_category_count": critical_category_count,
        "warning_category_count": warning_category_count,
        "entries": entries,
    }


@app.get("/api/maintenance-windows")
def api_maintenance_windows():
    """Return current maintenance work while surfacing incomplete evidence."""
    now = datetime.now(timezone.utc)
    tracked_site_count = len(store.list_sites())
    dashboard_rows = store.latest_dashboard()
    current_rows = _current_snapshot_rows(dashboard_rows, now)
    monitored_site_count = len(dashboard_rows)
    current_evidence_count = len(current_rows)
    missing_snapshot_count = tracked_site_count - monitored_site_count
    stale_snapshot_count = monitored_site_count - current_evidence_count
    unknown_count = tracked_site_count - current_evidence_count
    urgency_rank = {"immediate": 0, "scheduled": 1, "none": 2}
    sites = []
    for row in current_rows:
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
    immediate_count = sum(1 for site in sites if site["maintenance_window"] == "immediate")
    scheduled_count = sum(1 for site in sites if site["maintenance_window"] == "scheduled")
    status = "red" if immediate_count else ("yellow" if scheduled_count or unknown_count else "green")
    return {
        "generated_at": now.isoformat(),
        "status": status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": tracked_site_count,
        "monitored_site_count": monitored_site_count,
        "current_evidence_count": current_evidence_count,
        "missing_snapshot_count": missing_snapshot_count,
        "stale_snapshot_count": stale_snapshot_count,
        "unknown_count": unknown_count,
        "maintenance_evidence_percent": (
            round((current_evidence_count / tracked_site_count) * 100)
            if tracked_site_count
            else 100
        ),
        "window_count": len(sites),
        "immediate_count": immediate_count,
        "scheduled_count": scheduled_count,
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


def _maintenance_calendar_windows(rows: list[dict]) -> list[dict]:
    """Summarize maintenance work by timing window for planning views."""
    labels = {"immediate": "Immediate maintenance", "scheduled": "Scheduled maintenance"}
    urgency_rank = {"immediate": 0, "scheduled": 1}
    windows: dict[str, dict] = {}
    for row in rows:
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
    """Return current maintenance windows while surfacing incomplete evidence."""
    now = datetime.now(timezone.utc)
    tracked_site_count = len(store.list_sites())
    dashboard_rows = store.latest_dashboard()
    current_rows = _current_snapshot_rows(dashboard_rows, now)
    monitored_site_count = len(dashboard_rows)
    current_evidence_count = len(current_rows)
    missing_snapshot_count = tracked_site_count - monitored_site_count
    stale_snapshot_count = monitored_site_count - current_evidence_count
    unknown_count = tracked_site_count - current_evidence_count
    windows = _maintenance_calendar_windows(current_rows)
    immediate_site_count = sum(window["site_count"] for window in windows if window["window"] == "immediate")
    scheduled_site_count = sum(window["site_count"] for window in windows if window["window"] == "scheduled")
    status = "red" if immediate_site_count else (
        "yellow" if scheduled_site_count or unknown_count else "green"
    )
    return {
        "generated_at": now.isoformat(),
        "status": status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": tracked_site_count,
        "monitored_site_count": monitored_site_count,
        "current_evidence_count": current_evidence_count,
        "missing_snapshot_count": missing_snapshot_count,
        "stale_snapshot_count": stale_snapshot_count,
        "unknown_count": unknown_count,
        "maintenance_evidence_percent": (
            round((current_evidence_count / tracked_site_count) * 100)
            if tracked_site_count
            else 100
        ),
        "window_count": len(windows),
        "immediate_site_count": immediate_site_count,
        "scheduled_site_count": scheduled_site_count,
        "windows": windows,
    }


@app.get("/api/slo")
def api_slo():
    """Return fleet-level service objective compliance for leadership review."""
    now = datetime.now(timezone.utc)
    rows = store.latest_dashboard()
    current_rows = _current_snapshot_rows(rows, now)
    monitored_total = len(rows)
    tracked_total = len(store.list_sites())
    current_snapshot_count = len(current_rows)
    # Every objective uses the full tracked fleet as its denominator and only
    # current snapshots as evidence. A stale or missing snapshot therefore
    # cannot make an operational objective look healthy.
    objectives = [
        _slo_row("availability", "Sites reachable", tracked_total, sum(1 for row in current_rows if row["uptime_ok"]), "site reachable"),
        _slo_row("tls", "TLS renewal buffer", tracked_total, sum(1 for row in current_rows if row["ssl_days"] >= 14), ">= 14 days remaining"),
        _slo_row("backups", "Backup freshness", tracked_total, sum(1 for row in current_rows if row["backup_age_hours"] <= 72), "<= 72 hours old"),
        _slo_row("performance", "Homepage response", tracked_total, sum(1 for row in current_rows if row["response_ms"] <= 1500), "<= 1500 ms"),
        _slo_row("security", "Security headers", tracked_total, sum(1 for row in current_rows if row["security_header_count"] >= 2), ">= 2 core headers"),
        _slo_row(
            "monitoring",
            "Current monitoring evidence",
            tracked_total,
            current_snapshot_count,
            f"snapshot <= {SNAPSHOT_FRESHNESS_HOURS} hours old",
        ),
    ]
    objectives.sort(key=lambda objective: (objective["compliance_percent"], objective["name"]))
    worst_objective = objectives[0] if objectives else None
    return {
        "generated_at": now.isoformat(),
        "site_count": tracked_total,
        "monitored_site_count": monitored_total,
        "current_snapshot_count": current_snapshot_count,
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


def _client_digest_status(
    immediate_count: int,
    scheduled_count: int,
    average_score: int,
    monitoring_gap_count: int = 0,
) -> str:
    """Return a client-friendly status for an account digest."""
    if immediate_count:
        return "red"
    if scheduled_count or monitoring_gap_count or average_score < 85:
        return "yellow"
    return "green"


@app.get("/api/client-digest")
def api_client_digest():
    """Return client-level executive summaries for account check-ins."""
    now = datetime.now(timezone.utc)
    actions_by_client: dict[str, list[dict]] = {}
    for action in _current_actions():
        actions_by_client.setdefault(action.get("client") or "Unassigned", []).append(action)

    account_summaries = api_clients()["clients"]
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    clients = {
        account["client"]: {
            "client": account["client"],
            "site_count": account["site_count"],
            "monitored_site_count": account["monitored_site_count"],
            "current_snapshot_count": account["current_snapshot_count"],
            "missing_snapshot_count": account["missing_snapshot_count"],
            "stale_snapshot_count": account["stale_snapshot_count"],
            "monitoring_coverage_percent": account["monitoring_coverage_percent"],
            "snapshot_freshness_percent": account["snapshot_freshness_percent"],
            "average_score": account["average_score"],
            "latest_snapshot_at": account["latest_snapshot_at"],
            "sites": [],
        }
        for account in account_summaries
    }

    for site in store.list_sites():
        client_name = site.get("client") or "Unassigned"
        digest = clients[client_name]
        row = latest_by_url.get(site["url"])
        if row is None:
            digest["sites"].append(
                {
                    "name": site["name"],
                    "url": site["url"],
                    "score": None,
                    "status": "unknown",
                    "critical_alerts": 0,
                    "observed_score": None,
                    "observed_status": None,
                    "observed_critical_alerts": 0,
                    "monitoring_status": "missing_snapshot",
                    "snapshot_freshness": "missing",
                    "snapshot_age_hours": None,
                    "latest_snapshot_at": None,
                }
            )
            continue

        freshness, age_hours = _snapshot_freshness(
            row.get("captured_at"),
            now,
            SNAPSHOT_FRESHNESS_HOURS,
        )
        observed_score = row["score"]
        observed_status = _dashboard_status(observed_score)
        observed_critical_alerts = sum(
            1 for alert in row["alerts"] if alert.get("severity") == "critical"
        )
        is_current = freshness == "current"
        digest["sites"].append(
            {
                "name": row["name"],
                "url": row["url"],
                "score": observed_score if is_current else None,
                "status": observed_status if is_current else "unknown",
                "critical_alerts": observed_critical_alerts if is_current else 0,
                "observed_score": observed_score,
                "observed_status": observed_status,
                "observed_critical_alerts": observed_critical_alerts,
                "monitoring_status": "monitored",
                "snapshot_freshness": freshness,
                "snapshot_age_hours": age_hours,
                "latest_snapshot_at": row["captured_at"],
            }
        )

    digest_rows = []
    freshness_rank = {"missing": 0, "invalid": 1, "clock_skew": 2, "stale": 3, "current": 4}
    for client_name, digest in clients.items():
        actions = actions_by_client.get(client_name, [])
        immediate_count = sum(1 for action in actions if _remediation_bucket(action) == "immediate")
        scheduled_count = sum(1 for action in actions if _remediation_bucket(action) == "scheduled")
        watch_count = sum(1 for action in actions if _remediation_bucket(action) == "watch")
        monitoring_gap_count = digest["missing_snapshot_count"] + digest["stale_snapshot_count"]
        top_action = actions[0] if actions else None
        status = _client_digest_status(
            immediate_count,
            scheduled_count,
            digest["average_score"],
            monitoring_gap_count,
        )
        digest["status"] = status
        digest["immediate_action_count"] = immediate_count
        digest["scheduled_action_count"] = scheduled_count
        digest["watch_action_count"] = watch_count
        digest["open_action_count"] = len(actions)
        digest["top_site"] = top_action["site"] if top_action else None
        digest["top_message"] = top_action["message"] if top_action else "No open fleet actions."
        site_label = "site" if digest["site_count"] == 1 else "sites"
        snapshot_verb = "has" if digest["site_count"] == 1 else "have"
        digest["executive_summary"] = (
            f"{client_name} has {digest['site_count']} tracked {site_label}; "
            f"{digest['monitored_site_count']} of {digest['site_count']} tracked {site_label} {snapshot_verb} snapshots, "
            f"with an average score of {digest['average_score']} and "
            f"{len(actions)} open action{'s' if len(actions) != 1 else ''}."
        )
        digest["sites"].sort(
            key=lambda site: (
                freshness_rank.get(site["snapshot_freshness"], 99),
                site["score"] if site["score"] is not None else -1,
                site["name"].lower(),
            )
        )
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
        "generated_at": now.isoformat(),
        "client_count": len(digest_rows),
        "red_count": sum(1 for client in digest_rows if client["status"] == "red"),
        "yellow_count": sum(1 for client in digest_rows if client["status"] == "yellow"),
        "green_count": sum(1 for client in digest_rows if client["status"] == "green"),
        "missing_snapshot_count": sum(client["missing_snapshot_count"] for client in digest_rows),
        "stale_snapshot_count": sum(client["stale_snapshot_count"] for client in digest_rows),
        "clients": digest_rows,
    }


def _client_escalation_rows() -> list[dict]:
    """Group current critical incidents by client for escalation handoffs."""
    escalations: dict[str, dict] = {}
    for incident in (action for action in _current_actions() if action["severity"] == "critical"):
        client_name = incident.get("client") or "Unassigned"
        row = escalations.setdefault(
            client_name,
            {
                "client": client_name,
                "critical_incident_count": 0,
                "affected_sites": set(),
                "lowest_score": incident["score"],
                "latest_snapshot_at": None,
                "top_incident": incident,
                "incidents": [],
            },
        )
        row["critical_incident_count"] += 1
        row["affected_sites"].add(incident["site"])
        row["lowest_score"] = min(row["lowest_score"], incident["score"])
        captured_at = incident.get("latest_snapshot_at")
        if captured_at and (row["latest_snapshot_at"] is None or captured_at > row["latest_snapshot_at"]):
            row["latest_snapshot_at"] = captured_at
        if (incident["score"], incident["site"].lower(), incident["message"].lower()) < (
            row["top_incident"]["score"],
            row["top_incident"]["site"].lower(),
            row["top_incident"]["message"].lower(),
        ):
            row["top_incident"] = incident
        row["incidents"].append(incident)

    rows = []
    for row in escalations.values():
        row["affected_site_count"] = len(row.pop("affected_sites"))
        row["top_site"] = row["top_incident"]["site"]
        row["top_message"] = row["top_incident"]["message"]
        row["top_recommended_action"] = row["top_incident"]["recommended_action"]
        del row["top_incident"]
        row["incidents"].sort(key=lambda incident: (incident["score"], incident["site"].lower(), incident["message"].lower()))
        rows.append(row)

    rows.sort(
        key=lambda row: (
            -row["critical_incident_count"],
            -row["affected_site_count"],
            row["lowest_score"],
            row["client"].lower(),
        )
    )
    return rows


@app.get("/api/client-escalations")
def api_client_escalations():
    """Return client-level critical incident escalations for urgent follow-up."""
    clients = _client_escalation_rows()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "client_count": len(clients),
        "critical_incident_count": sum(client["critical_incident_count"] for client in clients),
        "affected_site_count": sum(client["affected_site_count"] for client in clients),
        "clients": clients,
    }


@app.get("/api/stale-snapshots")
def api_stale_snapshots(threshold_hours: int = SNAPSHOT_FRESHNESS_HOURS):
    """Return sites whose latest fleet snapshot is missing, invalid, or stale."""
    threshold_hours = max(threshold_hours, 1)
    now = datetime.now(timezone.utc)
    all_sites = store.list_sites()
    latest_by_url = {row["url"]: row for row in store.latest_dashboard()}
    sites = []
    for site in all_sites:
        row = latest_by_url.get(site["url"])
        captured_at = row.get("captured_at") if row else None
        freshness, age_hours = _snapshot_freshness(captured_at, now, threshold_hours)
        if row is not None and freshness == "current":
            continue
        if row is None:
            staleness_status = "missing"
            recommended_action = "Capture a fresh fleet snapshot and verify site health."
        elif freshness == "clock_skew":
            staleness_status = "clock_skew"
            recommended_action = "Correct the snapshot timestamp or source clock, then capture a fresh snapshot."
        elif freshness == "invalid":
            staleness_status = "invalid"
            recommended_action = "Repair the invalid snapshot timestamp, then capture a fresh snapshot."
        else:
            staleness_status = "stale"
            recommended_action = "Capture a fresh fleet snapshot and verify site health."
        sites.append(
            {
                "name": site["name"],
                "url": site["url"],
                "client": site.get("client") or "Unassigned",
                "latest_snapshot_at": captured_at,
                "snapshot_age_hours": age_hours,
                "staleness_status": staleness_status,
                "recommended_action": recommended_action,
            }
        )

    status_rank = {"missing": 0, "invalid": 1, "clock_skew": 2, "stale": 3}
    sites.sort(
        key=lambda site: (
            status_rank.get(site["staleness_status"], 99),
            -(site["snapshot_age_hours"] or 10**9),
            site["client"].lower(),
            site["name"].lower(),
        )
    )
    current_snapshot_count = len(all_sites) - len(sites)
    return {
        "generated_at": now.isoformat(),
        "threshold_hours": threshold_hours,
        "site_count": len(all_sites),
        "stale_count": len(sites),
        "missing_snapshot_count": sum(1 for site in sites if site["staleness_status"] == "missing"),
        "invalid_timestamp_count": sum(1 for site in sites if site["staleness_status"] == "invalid"),
        "clock_skew_count": sum(1 for site in sites if site["staleness_status"] == "clock_skew"),
        "current_snapshot_count": current_snapshot_count,
        "snapshot_coverage_percent": round((current_snapshot_count / len(all_sites)) * 100) if all_sites else 100,
        "sites": sites,
    }


def _executive_risk_rows() -> list[dict]:
    """Return compact account-level risk rows for leadership review."""
    action_counts: dict[str, dict[str, int]] = {}
    for action in _current_actions():
        client_name = action.get("client") or "Unassigned"
        counts = action_counts.setdefault(client_name, {"critical": 0, "warning": 0, "info": 0})
        severity = action.get("severity", "info")
        counts[severity] = counts.get(severity, 0) + 1

    clients = {
        account["client"]: {
            "client": account["client"],
            "site_count": account["site_count"],
            "monitored_site_count": account["monitored_site_count"],
            "current_snapshot_count": account["current_snapshot_count"],
            "missing_snapshot_count": account["missing_snapshot_count"],
            "stale_snapshot_count": account["stale_snapshot_count"],
            "monitoring_gap_count": account["missing_snapshot_count"] + account["stale_snapshot_count"],
            "average_score": account["average_score"],
            "lowest_score": None,
            "critical_site_count": 0,
            "latest_snapshot_at": account["latest_snapshot_at"],
        }
        for account in api_clients()["clients"]
    }
    for row in _current_snapshot_rows(store.latest_dashboard()):
        client_name = row.get("client") or "Unassigned"
        summary = clients[client_name]
        summary["lowest_score"] = (
            row["score"]
            if summary["lowest_score"] is None
            else min(summary["lowest_score"], row["score"])
        )
        summary["critical_site_count"] += 1 if any(alert.get("severity") == "critical" for alert in row["alerts"]) else 0

    rows = []
    for client_name, summary in clients.items():
        counts = action_counts.get(client_name, {"critical": 0, "warning": 0, "info": 0})
        open_action_count = sum(counts.values())
        summary["lowest_score"] = summary["lowest_score"] if summary["lowest_score"] is not None else 100
        if counts.get("critical", 0) or summary["critical_site_count"]:
            risk_level = "critical"
        elif counts.get("warning", 0) or summary["monitoring_gap_count"] or summary["average_score"] < 85:
            risk_level = "elevated"
        else:
            risk_level = "stable"
        summary.update(
            {
                "risk_level": risk_level,
                "open_action_count": open_action_count,
                "critical_action_count": counts.get("critical", 0),
                "warning_action_count": counts.get("warning", 0),
            }
        )
        rows.append(summary)

    risk_rank = {"critical": 0, "elevated": 1, "stable": 2}
    rows.sort(
        key=lambda row: (
            risk_rank.get(row["risk_level"], 99),
            -row["critical_action_count"],
            -row["warning_action_count"],
            row["lowest_score"],
            row["client"].lower(),
        )
    )
    return rows


@app.get("/api/executive-risks")
def api_executive_risks():
    """Return a leadership-friendly client risk summary."""
    clients = _executive_risk_rows()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "client_count": len(clients),
        "critical_client_count": sum(1 for client in clients if client["risk_level"] == "critical"),
        "elevated_client_count": sum(1 for client in clients if client["risk_level"] == "elevated"),
        "stable_client_count": sum(1 for client in clients if client["risk_level"] == "stable"),
        "clients": clients,
    }


def _fleet_brief_status(
    critical_clients: int,
    immediate_actions: int,
    open_actions: int,
    monitoring_gap_count: int = 0,
) -> str:
    """Return a single operating status for the current fleet brief."""
    if critical_clients or immediate_actions:
        return "red"
    if open_actions or monitoring_gap_count:
        return "yellow"
    return "green"


@app.get("/api/fleet-brief")
def api_fleet_brief():
    """Return an operator-ready brief with risk, action, and SLO highlights."""
    client_risks = _executive_risk_rows()
    actions = _current_actions()
    slo = api_slo()
    fleet_summary = api_summary()
    critical_clients = sum(1 for client in client_risks if client["risk_level"] == "critical")
    immediate_actions = sum(1 for action in actions if _remediation_bucket(action) == "immediate")
    monitoring_gap_count = fleet_summary["missing_snapshot_count"] + fleet_summary["stale_snapshot_count"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": _fleet_brief_status(critical_clients, immediate_actions, len(actions), monitoring_gap_count),
        "site_count": fleet_summary["sites"],
        "client_count": len(client_risks),
        "critical_client_count": critical_clients,
        "immediate_action_count": immediate_actions,
        "open_action_count": len(actions),
        "missing_snapshot_count": fleet_summary["missing_snapshot_count"],
        "stale_snapshot_count": fleet_summary["stale_snapshot_count"],
        "monitoring_gap_count": monitoring_gap_count,
        "at_risk_objective_count": slo["at_risk_count"],
        "worst_objective": slo["worst_objective"],
        "top_clients": client_risks[:5],
        "top_actions": actions[:5],
    }


def _operator_handoff_headline(
    status: str,
    critical_clients: int,
    immediate_actions: int,
    monitoring_gap_count: int = 0,
    open_actions: int = 0,
) -> str:
    """Return a concise human-readable summary for shift handoff."""
    if status == "red":
        client_label = "client" if critical_clients == 1 else "clients"
        action_label = "action" if immediate_actions == 1 else "actions"
        return (
            f"Red: {critical_clients} critical {client_label} and "
            f"{immediate_actions} immediate {action_label} require operator follow-up."
        )
    if status == "yellow" and monitoring_gap_count and not open_actions:
        gap_label = "gap" if monitoring_gap_count == 1 else "gaps"
        gap_verb = "requires" if monitoring_gap_count == 1 else "require"
        return f"Yellow: {monitoring_gap_count} monitoring {gap_label} {gap_verb} operator follow-up."
    if status == "yellow":
        return "Yellow: scheduled maintenance items remain open; review during the next maintenance window."
    return "Green: no open fleet actions are currently blocking the maintenance queue."


@app.get("/api/operator-handoff")
def api_operator_handoff():
    """Return a shift-handoff summary with top clients, actions, and next notes."""
    client_risks = _executive_risk_rows()
    actions = []
    for action in _current_actions():
        urgency = _remediation_bucket(action)
        actions.append({**action, "urgency": urgency, "due": _remediation_due(urgency)})
    slo = api_slo()
    fleet_summary = api_summary()
    critical_clients = sum(1 for client in client_risks if client["risk_level"] == "critical")
    immediate_actions = sum(1 for action in actions if action["urgency"] == "immediate")
    monitoring_gap_count = fleet_summary["missing_snapshot_count"] + fleet_summary["stale_snapshot_count"]
    status = _fleet_brief_status(critical_clients, immediate_actions, len(actions), monitoring_gap_count)
    top_clients = client_risks[:3]
    priority_clients = [client for client in client_risks if client["risk_level"] != "stable"]
    top_actions = actions[:5]
    if priority_clients:
        top_client = priority_clients[0]
        if top_client["monitoring_gap_count"] and not top_client["open_action_count"]:
            missing = top_client["missing_snapshot_count"]
            stale = top_client["stale_snapshot_count"]
            handoff_notes = [
                f"Restore monitoring coverage for {top_client['client']}: "
                f"{missing} missing snapshot{'s' if missing != 1 else ''} and "
                f"{stale} stale snapshot{'s' if stale != 1 else ''}."
            ]
        else:
            handoff_notes = [
                f"Prioritize {top_client['client']} due to "
                f"{top_client['critical_action_count']} critical actions and a "
                f"lowest score of {top_client['lowest_score']}."
            ]
    else:
        handoff_notes = ["No client-level risks require handoff at this time."]
    if top_actions:
        handoff_notes.append(f"Next action: {top_actions[0]['recommended_action']}")
    if slo.get("worst_objective") and slo["worst_objective"]["status"] in {"watch", "at_risk"}:
        objective = slo["worst_objective"]
        handoff_notes.append(
            f"Watch SLO objective: {objective['label']} at {objective['compliance_percent']}% compliance."
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "headline": _operator_handoff_headline(
            status,
            critical_clients,
            immediate_actions,
            monitoring_gap_count,
            len(actions),
        ),
        "site_count": fleet_summary["sites"],
        "client_count": len(client_risks),
        "critical_client_count": critical_clients,
        "immediate_action_count": immediate_actions,
        "open_action_count": len(actions),
        "missing_snapshot_count": fleet_summary["missing_snapshot_count"],
        "stale_snapshot_count": fleet_summary["stale_snapshot_count"],
        "monitoring_gap_count": monitoring_gap_count,
        "at_risk_objective_count": slo["at_risk_count"],
        "worst_objective": slo["worst_objective"],
        "top_clients": top_clients,
        "top_actions": top_actions,
        "handoff_notes": handoff_notes,
    }


def _site_scorecard_status(row: dict, badges: dict[str, str]) -> str:
    """Return a concise status for a site scorecard row."""
    if not row["uptime_ok"] or any(value == "critical" for value in badges.values()) or _dashboard_status(row["score"]) == "red":
        return "critical"
    if row["score"] < 85 or any(value in {"warning", "slow"} for value in badges.values()):
        return "warning"
    return "healthy"


def _site_scorecard_next_action(row: dict) -> str:
    """Return the highest-priority next step for a site's latest alert state."""
    if not row["alerts"]:
        return "Continue normal maintenance cadence."
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    top_alert = min(
        enumerate(row["alerts"]),
        key=lambda item: (severity_rank.get(item[1].get("severity", "info"), 99), item[0]),
    )[1]
    return _recommended_action(top_alert)


def _site_scorecard_evidence_action(freshness: str) -> str:
    """Return a next step without presenting incomplete evidence as current."""
    if freshness == "clock_skew":
        return "Correct the snapshot timestamp or source clock, then capture a fresh snapshot."
    if freshness == "invalid":
        return "Repair the invalid snapshot timestamp, then capture a fresh snapshot."
    return "Capture a fresh fleet snapshot before relying on this scorecard."


def _site_scorecard_rows() -> list[dict]:
    """Build compact cards without treating stale observations as current health."""
    now = datetime.now(timezone.utc)
    status_rank = {"unknown": 0, "critical": 1, "warning": 2, "healthy": 3}
    rows = []
    for row in store.latest_dashboard():
        observed_badges = {
            "availability": "healthy" if row["uptime_ok"] else "critical",
            "tls": _certificate_status(row["ssl_days"]),
            "updates": _update_status(row["wp_updates"]),
            "backups": _backup_status(row["backup_age_hours"]),
            "performance": _performance_status(row["response_ms"]),
            "security": _security_status(row["security_header_count"]),
        }
        observed_status = _site_scorecard_status(row, observed_badges)
        observed_alert_count = len(row["alerts"])
        freshness, age_hours = _snapshot_freshness(
            row.get("captured_at"),
            now,
            SNAPSHOT_FRESHNESS_HOURS,
        )
        is_current = freshness == "current"
        badges = observed_badges if is_current else dict.fromkeys(observed_badges, "unknown")
        rows.append(
            {
                "name": row["name"],
                "url": row["url"],
                "client": row.get("client") or "Unassigned",
                "score": row["score"] if is_current else None,
                "observed_score": row["score"],
                "status": observed_status if is_current else "unknown",
                "observed_status": observed_status,
                "badges": badges,
                "observed_badges": observed_badges,
                "alert_count": observed_alert_count if is_current else 0,
                "observed_alert_count": observed_alert_count,
                "next_action": (
                    _site_scorecard_next_action(row)
                    if is_current
                    else _site_scorecard_evidence_action(freshness)
                ),
                "latest_snapshot_at": row["captured_at"],
                "snapshot_freshness": freshness,
                "snapshot_age_hours": age_hours,
            }
        )
    rows.sort(key=lambda site: (status_rank.get(site["status"], 99), site["observed_score"], -site["alert_count"], site["client"].lower(), site["name"].lower()))
    return rows


@app.get("/api/site-scorecards")
def api_site_scorecards():
    """Return compact per-site operational status cards for portals and widgets."""
    sites = _site_scorecard_rows()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site_count": len(sites),
        "critical_count": sum(1 for site in sites if site["status"] == "critical"),
        "warning_count": sum(1 for site in sites if site["status"] == "warning"),
        "healthy_count": sum(1 for site in sites if site["status"] == "healthy"),
        "unknown_count": sum(1 for site in sites if site["status"] == "unknown"),
        "sites": sites,
    }


@app.get("/api/snapshot-history")
def api_snapshot_history(
    url: str | None = None,
    client: str | None = None,
    limit: int = 25,
    offset: int = 0,
):
    """Return paginated fleet snapshots, optionally filtered by site or client."""
    bounded_limit = max(1, min(limit, 100))
    normalized_url = normalize_site_url(url) if url is not None else None
    normalized_client = _normalize_client_filter(client)
    total_snapshot_count = store.count_snapshots(normalized_url, normalized_client)
    bounded_offset = _bounded_history_offset(bounded_limit, offset, total_snapshot_count)
    history_rows = store.recent_snapshots(
        bounded_limit,
        bounded_offset,
        normalized_url,
        normalized_client,
    )
    snapshots = [
        {
            "snapshot_id": row["id"],
            "name": row["name"],
            "url": row["url"],
            "client": row.get("client") or "Unassigned",
            "score": row["score"],
            "status": _dashboard_status(row["score"]),
            "captured_at": row["captured_at"],
            "uptime_ok": bool(row["uptime_ok"]),
            "ssl_days": row["ssl_days"],
            "wp_updates": row["wp_updates"],
            "backup_age_hours": row["backup_age_hours"],
            "response_ms": row["response_ms"],
            "security_header_count": row["security_header_count"],
            "alert_count": len(row["alerts"]),
            "alerts": row["alerts"],
        }
        for row in history_rows
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "url": normalized_url,
        "client": normalized_client,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "snapshot_count": len(snapshots),
        "total_snapshot_count": total_snapshot_count,
        **_pagination_navigation(
            bounded_limit,
            bounded_offset,
            len(snapshots),
            total_snapshot_count,
        ),
        "snapshots": snapshots,
    }


@app.get("/api/care-check-history")
def api_care_check_history(
    url: str | None = None,
    client: str | None = None,
    limit: int = 25,
    offset: int = 0,
):
    """Return paginated care checks, optionally filtered by site or client."""
    bounded_limit = max(1, min(limit, 100))
    normalized_url = normalize_site_url(url) if url is not None else None
    normalized_client = _normalize_client_filter(client)
    total_care_check_count = store.count_care_checks(normalized_url, normalized_client)
    bounded_offset = _bounded_history_offset(bounded_limit, offset, total_care_check_count)
    care_checks = [
        {
            "care_check_id": row["id"],
            "name": row["name"],
            "url": row["url"],
            "client": row.get("client") or "Unassigned",
            "status": row["status"],
            "score": row["score"],
            "checked_at": row["checked_at"],
            "http_status": row["http_status"],
            "latency_ms": row["latency_ms"],
            "ssl_days_remaining": row["ssl_days_remaining"],
            "wordpress_version": row["wordpress_version"],
            "update_count": row["update_count"],
            "backup_age_hours": row["backup_age_hours"],
            "summary": row["summary"],
            "actions": row["actions"],
            "security_headers": row["security_headers"],
        }
        for row in store.recent_care_checks(
            bounded_limit,
            bounded_offset,
            normalized_url,
            normalized_client,
        )
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "url": normalized_url,
        "client": normalized_client,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "care_check_count": len(care_checks),
        "total_care_check_count": total_care_check_count,
        **_pagination_navigation(
            bounded_limit,
            bounded_offset,
            len(care_checks),
            total_care_check_count,
        ),
        "care_checks": care_checks,
    }


@app.get("/api/site-snapshot-history")
def api_site_snapshot_history(url: str, limit: int = 25, offset: int = 0):
    """Return a paginated snapshot history for one site, newest first."""
    bounded_limit = max(1, min(limit, 100))
    normalized = normalize_site_url(url)
    total_snapshot_count = store.count_site_snapshots(normalized)
    bounded_offset = _bounded_history_offset(bounded_limit, offset, total_snapshot_count)
    snapshots = [
        {
            "snapshot_id": row["id"],
            "name": row["name"],
            "url": row["url"],
            "client": row.get("client") or "Unassigned",
            "score": row["score"],
            "status": _dashboard_status(row["score"]),
            "captured_at": row["captured_at"],
            "uptime_ok": bool(row["uptime_ok"]),
            "ssl_days": row["ssl_days"],
            "wp_updates": row["wp_updates"],
            "backup_age_hours": row["backup_age_hours"],
            "response_ms": row["response_ms"],
            "security_header_count": row["security_header_count"],
            "alert_count": len(row["alerts"]),
            "alerts": row["alerts"],
        }
        for row in store.site_snapshots(normalized, bounded_limit, bounded_offset)
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "url": normalized,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "snapshot_count": len(snapshots),
        "total_snapshot_count": total_snapshot_count,
        **_pagination_navigation(
            bounded_limit,
            bounded_offset,
            len(snapshots),
            total_snapshot_count,
        ),
        "snapshots": snapshots,
    }


def _site_trend_status(score_delta: int | None) -> str:
    """Return a compact trend label from a latest-vs-previous score delta."""
    if score_delta is None:
        return "new"
    if score_delta >= 5:
        return "improving"
    if score_delta <= -5:
        return "regressing"
    return "stable"


def _site_trend_evidence_action(freshness: str) -> str:
    """Return a safe next step when the latest trend evidence is not current."""
    if freshness == "clock_skew":
        return "Correct the latest snapshot timestamp or source clock before relying on site trend status."
    if freshness == "invalid":
        return "Repair the latest snapshot timestamp before relying on site trend status."
    return "Capture a fresh fleet snapshot before relying on site trend status."


def _site_trend_rows(limit: int) -> list[dict]:
    """Compare each site's latest snapshot with its prior snapshot for trend triage."""
    now = datetime.now(timezone.utc)
    history_by_url: dict[str, list[dict]] = {}
    for snapshot in store.recent_trend_snapshots(limit):
        history_by_url.setdefault(snapshot["url"], []).append(snapshot)

    rows = []
    for snapshots in history_by_url.values():
        latest = snapshots[0]
        previous = snapshots[1] if len(snapshots) > 1 else None
        observed_score_delta = latest["score"] - previous["score"] if previous else None
        observed_status = _site_trend_status(observed_score_delta)
        freshness, age_hours = _snapshot_freshness(
            latest.get("captured_at"),
            now,
            SNAPSHOT_FRESHNESS_HOURS,
        )
        is_current = freshness == "current"
        rows.append(
            {
                "name": latest["name"],
                "url": latest["url"],
                "client": latest.get("client") or "Unassigned",
                "latest_score": latest["score"] if is_current else None,
                "previous_score": previous["score"] if previous and is_current else None,
                "score_delta": observed_score_delta if is_current else None,
                "trend_status": observed_status if is_current else "unknown",
                "observed_latest_score": latest["score"],
                "observed_previous_score": previous["score"] if previous else None,
                "observed_score_delta": observed_score_delta,
                "observed_trend_status": observed_status,
                "snapshot_freshness": freshness,
                "snapshot_age_hours": age_hours,
                "latest_snapshot_at": latest["captured_at"],
                "previous_snapshot_at": previous["captured_at"] if previous else None,
                "recommended_action": (
                    "Review recent changes and open a remediation task for the regression."
                    if is_current and observed_status == "regressing"
                    else (
                        "Continue monitoring the site trend."
                        if is_current
                        else _site_trend_evidence_action(freshness)
                    )
                ),
            }
        )

    trend_rank = {"unknown": 0, "regressing": 1, "new": 2, "stable": 3, "improving": 4}
    rows.sort(
        key=lambda row: (
            trend_rank.get(row["trend_status"], 99),
            row["observed_score_delta"] if row["observed_score_delta"] is not None else 0,
            row["observed_latest_score"],
            row["client"].lower(),
            row["name"].lower(),
        )
    )
    return rows


@app.get("/api/site-trends")
def api_site_trends(limit: int = 100):
    """Return latest-vs-previous site score trends for dispatch planning."""
    bounded_limit = max(2, min(limit, 500))
    trends = _site_trend_rows(bounded_limit)
    current_evidence_count = sum(
        1 for trend in trends if trend["snapshot_freshness"] == "current"
    )
    unknown_count = len(trends) - current_evidence_count
    regressing_count = sum(1 for trend in trends if trend["trend_status"] == "regressing")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "red" if regressing_count else ("yellow" if unknown_count else "green"),
        "snapshot_limit": bounded_limit,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "site_count": len(trends),
        "current_evidence_count": current_evidence_count,
        "unknown_count": unknown_count,
        "trend_evidence_percent": round((current_evidence_count / len(trends)) * 100) if trends else 100,
        "regressing_count": regressing_count,
        "improving_count": sum(1 for trend in trends if trend["trend_status"] == "improving"),
        "new_count": sum(1 for trend in trends if trend["trend_status"] == "new"),
        "trends": trends,
    }


def _action_matrix_rows() -> list[dict]:
    """Group open actions by client and site for dispatch planning."""
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    clients: dict[str, dict] = {}
    for action in _current_actions():
        client_name = action.get("client") or "Unassigned"
        client = clients.setdefault(
            client_name,
            {
                "client": client_name,
                "open_action_count": 0,
                "critical_action_count": 0,
                "warning_action_count": 0,
                "info_action_count": 0,
                "lowest_score": action["score"],
                "latest_snapshot_at": None,
                "sites": {},
            },
        )
        severity = action.get("severity", "info")
        client["open_action_count"] += 1
        client[f"{severity}_action_count"] += 1
        client["lowest_score"] = min(client["lowest_score"], action["score"])
        captured_at = action.get("latest_snapshot_at")
        if captured_at and (client["latest_snapshot_at"] is None or captured_at > client["latest_snapshot_at"]):
            client["latest_snapshot_at"] = captured_at

        site = client["sites"].setdefault(
            action["site"],
            {
                "site": action["site"],
                "url": action["url"],
                "score": action["score"],
                "open_action_count": 0,
                "critical_action_count": 0,
                "warning_action_count": 0,
                "info_action_count": 0,
                "top_severity": severity,
                "top_message": action["message"],
                "top_recommended_action": action["recommended_action"],
            },
        )
        site["open_action_count"] += 1
        site[f"{severity}_action_count"] += 1
        if severity_rank.get(severity, 99) < severity_rank.get(site["top_severity"], 99):
            site["top_severity"] = severity
            site["top_message"] = action["message"]
            site["top_recommended_action"] = action["recommended_action"]

    rows = []
    for client in clients.values():
        sites = list(client.pop("sites").values())
        sites.sort(
            key=lambda site: (
                severity_rank.get(site["top_severity"], 99),
                -site["open_action_count"],
                site["score"],
                site["site"].lower(),
            )
        )
        client["site_count"] = len(sites)
        client["sites"] = sites
        rows.append(client)
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


@app.get("/api/action-matrix")
def api_action_matrix():
    """Return open actions grouped by client and site for dispatch planning."""
    clients = _action_matrix_rows()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "client_count": len(clients),
        "site_count": sum(client["site_count"] for client in clients),
        "open_action_count": sum(client["open_action_count"] for client in clients),
        "critical_action_count": sum(client["critical_action_count"] for client in clients),
        "warning_action_count": sum(client["warning_action_count"] for client in clients),
        "clients": clients,
    }


def _site_priority_score(row: dict) -> int:
    """Return a dispatch score that favors urgent, multi-signal site risk."""
    critical_alerts = sum(1 for alert in row["alerts"] if alert.get("severity") == "critical")
    warning_alerts = sum(1 for alert in row["alerts"] if alert.get("severity") == "warning")
    info_alerts = sum(1 for alert in row["alerts"] if alert.get("severity") == "info")
    score_gap = max(0, 85 - (row["score"] or 0))
    freshness_penalty = 20 if row["backup_age_hours"] > 72 else (10 if row["backup_age_hours"] > 36 else 0)
    update_penalty = 15 if row["wp_updates"] >= 5 else (5 if row["wp_updates"] > 0 else 0)
    return critical_alerts * 100 + warning_alerts * 25 + info_alerts * 5 + score_gap + freshness_penalty + update_penalty


@app.get("/api/site-priorities")
def api_site_priorities(limit: int = 10):
    """Return current site priorities plus coverage needed to trust the queue."""
    bounded_limit = max(1, min(limit, 50))
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    dashboard_rows = store.latest_dashboard()
    tracked_sites = store.list_sites()
    current_rows = _current_snapshot_rows(dashboard_rows)
    missing_snapshot_count = max(len(tracked_sites) - len(dashboard_rows), 0)
    stale_snapshot_count = len(dashboard_rows) - len(current_rows)
    monitoring_gap_count = missing_snapshot_count + stale_snapshot_count
    sites = []
    for row in current_rows:
        critical_alerts = sum(1 for alert in row["alerts"] if alert.get("severity") == "critical")
        warning_alerts = sum(1 for alert in row["alerts"] if alert.get("severity") == "warning")
        top_alert = (
            min(
                enumerate(row["alerts"]),
                key=lambda item: (severity_rank.get(item[1].get("severity", "info"), 99), item[0]),
            )[1]
            if row["alerts"]
            else None
        )
        priority_score = _site_priority_score(row)
        if priority_score <= 0:
            continue
        sites.append(
            {
                "name": row["name"],
                "url": row["url"],
                "client": row.get("client") or "Unassigned",
                "score": row["score"],
                "priority_score": priority_score,
                "critical_alert_count": critical_alerts,
                "warning_alert_count": warning_alerts,
                "top_alert": top_alert.get("message") if top_alert else "Score is below target.",
                "top_severity": top_alert.get("severity") if top_alert else "info",
                "next_action": _recommended_action(top_alert or {}),
                "latest_snapshot_at": row["captured_at"],
            }
        )

    sites.sort(key=lambda site: (-site["priority_score"], severity_rank.get(site["top_severity"], 99), site["score"], site["client"].lower(), site["name"].lower()))
    selected = sites[:bounded_limit]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": bounded_limit,
        "site_count": len(tracked_sites),
        "monitored_site_count": len(dashboard_rows),
        "current_snapshot_count": len(current_rows),
        "missing_snapshot_count": missing_snapshot_count,
        "stale_snapshot_count": stale_snapshot_count,
        "monitoring_gap_count": monitoring_gap_count,
        "priority_evidence_percent": (
            round((len(current_rows) / len(tracked_sites)) * 100) if tracked_sites else 100
        ),
        "priority_site_count": len(sites),
        "returned_site_count": len(selected),
        "sites": selected,
    }


@app.get("/api/client-priorities")
def api_client_priorities(limit: int = 10):
    """Return current client priorities plus coverage needed to trust the queue."""
    bounded_limit = max(1, min(limit, 50))
    now = datetime.now(timezone.utc)
    tracked_sites = store.list_sites()
    dashboard_rows = store.latest_dashboard()
    latest_by_url = {row["url"]: row for row in dashboard_rows}
    current_rows = _current_snapshot_rows(dashboard_rows, now)
    tracked_clients = {site.get("client") or "Unassigned" for site in tracked_sites}
    monitored_clients = {row.get("client") or "Unassigned" for row in dashboard_rows}
    monitoring_gap_clients = {
        site.get("client") or "Unassigned"
        for site in tracked_sites
        if site["url"] not in latest_by_url
        or not _snapshot_is_current(
            latest_by_url[site["url"]].get("captured_at"),
            now,
            SNAPSHOT_FRESHNESS_HOURS,
        )
    }
    missing_snapshot_count = max(len(tracked_sites) - len(dashboard_rows), 0)
    stale_snapshot_count = len(dashboard_rows) - len(current_rows)
    monitoring_gap_count = missing_snapshot_count + stale_snapshot_count
    clients: dict[str, dict] = {}
    for row in current_rows:
        priority_score = _site_priority_score(row)
        if priority_score <= 0:
            continue
        client_name = row.get("client") or "Unassigned"
        critical_alerts = sum(1 for alert in row["alerts"] if alert.get("severity") == "critical")
        warning_alerts = sum(1 for alert in row["alerts"] if alert.get("severity") == "warning")
        summary = clients.setdefault(
            client_name,
            {
                "client": client_name,
                "priority_score": 0,
                "priority_site_count": 0,
                "critical_alert_count": 0,
                "warning_alert_count": 0,
                "lowest_score": row["score"],
                "latest_snapshot_at": None,
                "top_site": None,
                "top_site_priority_score": 0,
            },
        )
        summary["priority_score"] += priority_score
        summary["priority_site_count"] += 1
        summary["critical_alert_count"] += critical_alerts
        summary["warning_alert_count"] += warning_alerts
        summary["lowest_score"] = min(summary["lowest_score"], row["score"])
        captured_at = row.get("captured_at")
        if captured_at and (summary["latest_snapshot_at"] is None or captured_at > summary["latest_snapshot_at"]):
            summary["latest_snapshot_at"] = captured_at
        if priority_score > summary["top_site_priority_score"]:
            summary["top_site"] = row["name"]
            summary["top_site_priority_score"] = priority_score

    rows = list(clients.values())
    rows.sort(
        key=lambda client: (
            -client["priority_score"],
            -client["critical_alert_count"],
            -client["warning_alert_count"],
            client["lowest_score"],
            client["client"].lower(),
        )
    )
    selected = rows[:bounded_limit]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": bounded_limit,
        "tracked_client_count": len(tracked_clients),
        "monitored_client_count": len(monitored_clients),
        "monitoring_gap_client_count": len(monitoring_gap_clients),
        "tracked_site_count": len(tracked_sites),
        "monitored_site_count": len(dashboard_rows),
        "current_snapshot_count": len(current_rows),
        "missing_snapshot_count": missing_snapshot_count,
        "stale_snapshot_count": stale_snapshot_count,
        "monitoring_gap_count": monitoring_gap_count,
        "priority_evidence_percent": (
            round((len(current_rows) / len(tracked_sites)) * 100) if tracked_sites else 100
        ),
        "client_count": len(rows),
        "returned_client_count": len(selected),
        "total_priority_score": sum(client["priority_score"] for client in rows),
        "clients": selected,
    }


def _operations_kpi_status(
    immediate_actions: int,
    scheduled_actions: int,
    average_score: int,
    monitoring_gap_count: int = 0,
) -> str:
    """Return a fleet KPI status for management dashboards."""
    if immediate_actions:
        return "red"
    if scheduled_actions or monitoring_gap_count or average_score < 85:
        return "yellow"
    return "green"


@app.get("/api/operations-kpis")
def api_operations_kpis():
    """Return compact fleet KPIs for status pages and recurring ops reports."""
    rows = _current_snapshot_rows(store.latest_dashboard())
    fleet_summary = api_summary()
    actions = _current_actions()
    average_score = round(sum(row["score"] or 0 for row in rows) / len(rows)) if rows else 100
    immediate_actions = [action for action in actions if _remediation_bucket(action) == "immediate"]
    scheduled_actions = [action for action in actions if _remediation_bucket(action) == "scheduled"]
    watch_actions = [action for action in actions if _remediation_bucket(action) == "watch"]
    approval_packets = _maintenance_approval_packet_rows()
    priority_sites = [row for row in rows if _site_priority_score(row) > 0]
    priority_sites.sort(key=lambda row: (-_site_priority_score(row), row["score"], row["name"].lower()))
    monitoring_gap_count = fleet_summary["missing_snapshot_count"] + fleet_summary["stale_snapshot_count"]
    status = _operations_kpi_status(
        len(immediate_actions),
        len(scheduled_actions),
        average_score,
        monitoring_gap_count,
    )
    if immediate_actions:
        recommended_focus = immediate_actions[0]["recommended_action"]
    elif scheduled_actions:
        recommended_focus = scheduled_actions[0]["recommended_action"]
    elif fleet_summary["missing_snapshot_count"]:
        recommended_focus = "Capture initial fleet snapshots for unmonitored sites."
    elif fleet_summary["stale_snapshot_count"]:
        recommended_focus = "Refresh stale fleet snapshots before the next operations review."
    else:
        recommended_focus = "Continue normal monitoring cadence."
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "site_count": fleet_summary["sites"],
        "monitored_site_count": fleet_summary["monitored_site_count"],
        "missing_snapshot_count": fleet_summary["missing_snapshot_count"],
        "stale_snapshot_count": fleet_summary["stale_snapshot_count"],
        "monitoring_coverage_percent": fleet_summary["monitoring_coverage_percent"],
        "snapshot_freshness_percent": fleet_summary["snapshot_freshness_percent"],
        "average_score": average_score,
        "green_site_count": sum(1 for row in rows if _dashboard_status(row["score"]) == "green"),
        "yellow_site_count": sum(1 for row in rows if _dashboard_status(row["score"]) == "yellow"),
        "red_site_count": sum(1 for row in rows if _dashboard_status(row["score"]) == "red"),
        "open_action_count": len(actions),
        "immediate_action_count": len(immediate_actions),
        "scheduled_action_count": len(scheduled_actions),
        "watch_action_count": len(watch_actions),
        "approval_needed_count": sum(1 for packet in approval_packets if packet["packet_needed"]),
        "priority_site_count": len(priority_sites),
        "top_priority_site": priority_sites[0]["name"] if priority_sites else None,
        "recommended_focus": recommended_focus,
    }


def _client_update_brief_rows() -> list[dict]:
    """Return client-facing status briefs with current wins, risks, and next steps."""
    status_rank = {"red": 0, "yellow": 1, "green": 2}
    actions_by_client: dict[str, list[dict]] = {}
    for action in _current_actions():
        actions_by_client.setdefault(action.get("client") or "Unassigned", []).append(action)

    briefs = []
    for digest in api_client_digest()["clients"]:
        client_name = digest["client"]
        actions = actions_by_client.get(client_name, [])
        immediate_actions = [action for action in actions if _remediation_bucket(action) == "immediate"]
        scheduled_actions = [action for action in actions if _remediation_bucket(action) == "scheduled"]
        monitoring_gap_count = digest["missing_snapshot_count"] + digest["stale_snapshot_count"]
        healthy_site_count = sum(
            1
            for site in digest["sites"]
            if site["snapshot_freshness"] == "current"
            and site["score"] is not None
            and site["score"] >= 85
        )
        status = _client_digest_status(
            len(immediate_actions),
            len(scheduled_actions),
            digest["average_score"],
            monitoring_gap_count,
        )
        top_action = actions[0] if actions else None
        gap_site = next(
            (site for site in digest["sites"] if site["snapshot_freshness"] != "current"),
            None,
        )
        if top_action:
            next_action = top_action["recommended_action"]
            top_site = top_action["site"]
        elif digest["missing_snapshot_count"]:
            next_action = "Capture initial fleet snapshots for unmonitored sites."
            top_site = gap_site["name"] if gap_site else None
        elif digest["stale_snapshot_count"]:
            next_action = "Refresh stale fleet snapshots before the next client update."
            top_site = gap_site["name"] if gap_site else None
        else:
            next_action = "Continue normal monitoring cadence."
            top_site = None

        if monitoring_gap_count:
            site_label = "site" if digest["site_count"] == 1 else "sites"
            snapshot_verb = "has" if digest["site_count"] == 1 else "have"
            gap_label = "gap" if monitoring_gap_count == 1 else "gaps"
            gap_verb = "requires" if monitoring_gap_count == 1 else "require"
            client_message = (
                f"{digest['current_snapshot_count']} of {digest['site_count']} tracked {site_label} "
                f"{snapshot_verb} current snapshots; {monitoring_gap_count} monitoring {gap_label} {gap_verb} follow-up."
            )
        else:
            healthy_site_label = "site" if healthy_site_count == 1 else "sites"
            healthy_site_verb = "is" if healthy_site_count == 1 else "are"
            action_label = "action" if len(actions) == 1 else "actions"
            action_verb = "remains" if len(actions) == 1 else "remain"
            client_message = (
                f"{healthy_site_count} {healthy_site_label} {healthy_site_verb} healthy; "
                f"{len(actions)} open {action_label} {action_verb} in the work queue."
            )

        briefs.append(
            {
                "client": client_name,
                "site_count": digest["site_count"],
                "monitored_site_count": digest["monitored_site_count"],
                "current_snapshot_count": digest["current_snapshot_count"],
                "missing_snapshot_count": digest["missing_snapshot_count"],
                "stale_snapshot_count": digest["stale_snapshot_count"],
                "monitoring_coverage_percent": digest["monitoring_coverage_percent"],
                "snapshot_freshness_percent": digest["snapshot_freshness_percent"],
                "average_score": digest["average_score"],
                "healthy_site_count": healthy_site_count,
                "latest_snapshot_at": digest["latest_snapshot_at"],
                "status": status,
                "open_action_count": len(actions),
                "immediate_action_count": len(immediate_actions),
                "scheduled_action_count": len(scheduled_actions),
                "headline": (
                    f"{client_name}: {status.upper()} status across {digest['site_count']} tracked site"
                    f"{'s' if digest['site_count'] != 1 else ''}."
                ),
                "client_message": client_message,
                "next_action": next_action,
                "top_site": top_site,
            }
        )

    briefs.sort(
        key=lambda row: (
            status_rank.get(row["status"], 99),
            -row["immediate_action_count"],
            -row["scheduled_action_count"],
            row["average_score"],
            row["client"].lower(),
        )
    )
    return briefs


@app.get("/api/client-update-briefs")
def api_client_update_briefs():
    """Return client-facing account updates for emails, calls, and ticket notes."""
    clients = _client_update_brief_rows()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "client_count": len(clients),
        "red_count": sum(1 for client in clients if client["status"] == "red"),
        "yellow_count": sum(1 for client in clients if client["status"] == "yellow"),
        "green_count": sum(1 for client in clients if client["status"] == "green"),
        "missing_snapshot_count": sum(client["missing_snapshot_count"] for client in clients),
        "stale_snapshot_count": sum(client["stale_snapshot_count"] for client in clients),
        "clients": clients,
    }


def _client_service_review_rows() -> list[dict]:
    """Return account review rows with meeting-ready talking points."""
    topic_map = {
        "red": "Review urgent incidents, backup readiness, and maintenance approvals.",
        "yellow": "Review scheduled maintenance timing and open work queue ownership.",
        "green": "Review monitoring coverage, recent wins, and upcoming maintenance cadence.",
    }
    rows = []
    for brief in _client_update_brief_rows():
        action_count = brief["open_action_count"]
        monitoring_gap_count = brief["missing_snapshot_count"] + brief["stale_snapshot_count"]
        if brief["immediate_action_count"]:
            review_priority = "urgent"
        elif brief["scheduled_action_count"] or monitoring_gap_count or brief["average_score"] < 85:
            review_priority = "scheduled"
        else:
            review_priority = "routine"

        talking_point = (
            "Restore monitoring coverage before the next client review."
            if monitoring_gap_count
            else topic_map.get(brief["status"], topic_map["yellow"])
        )

        rows.append(
            {
                "client": brief["client"],
                "status": brief["status"],
                "review_priority": review_priority,
                "site_count": brief["site_count"],
                "current_snapshot_count": brief["current_snapshot_count"],
                "missing_snapshot_count": brief["missing_snapshot_count"],
                "stale_snapshot_count": brief["stale_snapshot_count"],
                "monitoring_gap_count": monitoring_gap_count,
                "average_score": brief["average_score"],
                "healthy_site_count": brief["healthy_site_count"],
                "open_action_count": action_count,
                "immediate_action_count": brief["immediate_action_count"],
                "scheduled_action_count": brief["scheduled_action_count"],
                "top_site": brief["top_site"],
                "talking_point": talking_point,
                "next_action": brief["next_action"],
                "latest_snapshot_at": brief["latest_snapshot_at"],
            }
        )
    priority_rank = {"urgent": 0, "scheduled": 1, "routine": 2}
    rows.sort(
        key=lambda row: (
            priority_rank.get(row["review_priority"], 99),
            -row["open_action_count"],
            row["average_score"],
            row["client"].lower(),
        )
    )
    return rows


@app.get("/api/client-service-reviews")
def api_client_service_reviews():
    """Return account-service review priorities for recurring client check-ins."""
    clients = _client_service_review_rows()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "client_count": len(clients),
        "urgent_review_count": sum(1 for client in clients if client["review_priority"] == "urgent"),
        "scheduled_review_count": sum(1 for client in clients if client["review_priority"] == "scheduled"),
        "routine_review_count": sum(1 for client in clients if client["review_priority"] == "routine"),
        "monitoring_gap_client_count": sum(1 for client in clients if client["monitoring_gap_count"]),
        "clients": clients,
    }


def _follow_up_channel(priority: str, open_action_count: int) -> str:
    """Return the suggested client touchpoint channel for a follow-up item."""
    if priority == "urgent":
        return "phone"
    if priority == "scheduled" or open_action_count:
        return "ticket"
    return "email"


def _follow_up_due(priority: str) -> str:
    """Return a human-friendly due bucket for client follow-up planning."""
    if priority == "urgent":
        return "today"
    if priority == "scheduled":
        return "this week"
    return "next account review"


@app.get("/api/client-follow-ups")
def api_client_follow_ups():
    """Return client follow-up prompts with channel and due-date guidance."""
    follow_ups = []
    for review in _client_service_review_rows():
        priority = review["review_priority"]
        follow_ups.append(
            {
                "client": review["client"],
                "priority": priority,
                "status": review["status"],
                "due": _follow_up_due(priority),
                "channel": _follow_up_channel(priority, review["open_action_count"]),
                "site_count": review["site_count"],
                "open_action_count": review["open_action_count"],
                "top_site": review["top_site"],
                "talking_point": review["talking_point"],
                "next_action": review["next_action"],
                "latest_snapshot_at": review["latest_snapshot_at"],
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "follow_up_count": len(follow_ups),
        "urgent_count": sum(1 for item in follow_ups if item["priority"] == "urgent"),
        "scheduled_count": sum(1 for item in follow_ups if item["priority"] == "scheduled"),
        "routine_count": sum(1 for item in follow_ups if item["priority"] == "routine"),
        "follow_ups": follow_ups,
    }


def _approval_packet_window(priority: str) -> str:
    """Return the safest approval timing window for client-facing maintenance work."""
    if priority == "urgent":
        return "same-day approval"
    if priority == "scheduled":
        return "next maintenance window"
    return "next account review"


def _maintenance_approval_packet_rows() -> list[dict]:
    """Return concise maintenance approval packets for account managers."""
    packets = []
    for review in _client_service_review_rows():
        priority = review["review_priority"]
        # Service reviews also schedule follow-up for stale or missing monitoring
        # data. That operational follow-up does not authorize maintenance on a
        # client site, so only create an approval request for actual open work.
        packet_needed = review["open_action_count"] > 0
        if packet_needed:
            approval_summary = (
                f"Request {priority} maintenance approval for {review['client']} covering "
                f"{review['open_action_count']} open action"
                f"{'s' if review['open_action_count'] != 1 else ''}."
            )
        else:
            approval_summary = f"No maintenance approval packet is needed for {review['client']} right now."
        packets.append(
            {
                "client": review["client"],
                "approval_priority": priority,
                "approval_window": _approval_packet_window(priority),
                "packet_needed": packet_needed,
                "site_count": review["site_count"],
                "top_site": review["top_site"],
                "open_action_count": review["open_action_count"],
                "immediate_action_count": review["immediate_action_count"],
                "scheduled_action_count": review["scheduled_action_count"],
                "talking_point": review["talking_point"],
                "approval_summary": approval_summary,
                "next_action": review["next_action"],
                "latest_snapshot_at": review["latest_snapshot_at"],
            }
        )
    return packets


@app.get("/api/maintenance-approval-packets")
def api_maintenance_approval_packets():
    """Return client maintenance approval packets for account-manager handoff."""
    packets = _maintenance_approval_packet_rows()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "packet_count": len(packets),
        "needed_count": sum(1 for packet in packets if packet["packet_needed"]),
        "urgent_count": sum(1 for packet in packets if packet["approval_priority"] == "urgent"),
        "scheduled_count": sum(1 for packet in packets if packet["approval_priority"] == "scheduled"),
        "packets": packets,
    }


def _approval_ticket_body(packet: dict) -> str:
    """Return a concise client-ticket body for maintenance approval requests."""
    top_site = packet["top_site"] or "the monitored site portfolio"
    return (
        f"{packet['approval_summary']} Top site: {top_site}. "
        f"Recommended next step: {packet['next_action']} "
        f"Suggested timing: {packet['approval_window']}."
    )


@app.get("/api/maintenance-ticket-drafts")
def api_maintenance_ticket_drafts():
    """Return ticket-ready maintenance approval drafts for account managers."""
    drafts = []
    for packet in _maintenance_approval_packet_rows():
        if not packet["packet_needed"]:
            continue
        drafts.append(
            {
                "client": packet["client"],
                "priority": packet["approval_priority"],
                "approval_window": packet["approval_window"],
                "subject": f"{packet['client']}: {packet['approval_priority'].title()} maintenance approval request",
                "body": _approval_ticket_body(packet),
                "top_site": packet["top_site"],
                "open_action_count": packet["open_action_count"],
                "latest_snapshot_at": packet["latest_snapshot_at"],
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "draft_count": len(drafts),
        "urgent_count": sum(1 for draft in drafts if draft["priority"] == "urgent"),
        "scheduled_count": sum(1 for draft in drafts if draft["priority"] == "scheduled"),
        "drafts": drafts,
    }


def _dispatch_summary_status(
    immediate_count: int,
    scheduled_count: int,
    monitoring_gap_count: int = 0,
) -> str:
    """Return a compact dispatch status for queue-level routing."""
    if immediate_count:
        return "red"
    if scheduled_count or monitoring_gap_count:
        return "yellow"
    return "green"


@app.get("/api/dispatch-summary")
def api_dispatch_summary():
    """Return a queue-level dispatch summary for daily operator routing."""
    actions = _current_actions()
    immediate_actions = [action for action in actions if _remediation_bucket(action) == "immediate"]
    scheduled_actions = [action for action in actions if _remediation_bucket(action) == "scheduled"]
    watch_actions = [action for action in actions if _remediation_bucket(action) == "watch"]
    priority_payload = api_site_priorities(limit=50)
    priority_sites = priority_payload["sites"][:5]
    client_workload = _client_workload_rows()
    top_client = client_workload[0] if client_workload else None
    top_action = actions[0] if actions else None
    monitoring_payload = api_stale_snapshots()
    monitoring_sites = monitoring_payload["sites"][:5]
    top_monitoring_site = monitoring_sites[0] if monitoring_sites else None
    monitoring_gap_count = monitoring_payload["stale_count"]
    missing_snapshot_count = monitoring_payload["missing_snapshot_count"]
    stale_snapshot_count = monitoring_gap_count - missing_snapshot_count
    if top_action:
        dispatch_top_client = top_client["client"] if top_client else top_action.get("client")
        dispatch_top_site = priority_sites[0]["name"] if priority_sites else top_action["site"]
        dispatch_top_action = top_action["recommended_action"]
    elif top_monitoring_site:
        dispatch_top_client = top_monitoring_site["client"]
        dispatch_top_site = top_monitoring_site["name"]
        dispatch_top_action = top_monitoring_site["recommended_action"]
    else:
        dispatch_top_client = None
        dispatch_top_site = None
        dispatch_top_action = "Continue normal monitoring cadence."

    if immediate_actions:
        next_queue = "immediate"
    elif scheduled_actions:
        next_queue = "scheduled"
    elif monitoring_gap_count:
        next_queue = "monitoring"
    elif watch_actions:
        next_queue = "watch"
    else:
        next_queue = "none"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": _dispatch_summary_status(
            len(immediate_actions),
            len(scheduled_actions),
            monitoring_gap_count,
        ),
        "open_action_count": len(actions),
        "immediate_action_count": len(immediate_actions),
        "scheduled_action_count": len(scheduled_actions),
        "watch_action_count": len(watch_actions),
        "missing_snapshot_count": missing_snapshot_count,
        "stale_snapshot_count": stale_snapshot_count,
        "monitoring_gap_count": monitoring_gap_count,
        "priority_site_count": priority_payload["priority_site_count"],
        "top_client": dispatch_top_client,
        "top_client_open_action_count": top_client["open_action_count"] if top_client else 0,
        "top_site": dispatch_top_site,
        "top_action": dispatch_top_action,
        "next_queue": next_queue,
        "priority_sites": priority_sites,
        "monitoring_sites": monitoring_sites,
    }


def _daily_ops_headline(
    status: str,
    immediate_count: int,
    scheduled_count: int,
    monitoring_gap_count: int,
    top_site: str | None,
) -> str:
    """Return a short shift headline for operator briefings."""
    if immediate_count:
        site_fragment = f" Start with {top_site}." if top_site else ""
        return f"Red shift brief: {immediate_count} immediate action{'s' if immediate_count != 1 else ''} need follow-up.{site_fragment}"
    if scheduled_count:
        return f"Yellow shift brief: {scheduled_count} scheduled action{'s' if scheduled_count != 1 else ''} should be planned."
    if monitoring_gap_count:
        gap_label = "gap needs" if monitoring_gap_count == 1 else "gaps need"
        site_fragment = f" Start with {top_site}." if top_site else ""
        return f"Yellow shift brief: {monitoring_gap_count} monitoring {gap_label} follow-up.{site_fragment}"
    if status == "green":
        return "Green shift brief: no urgent or scheduled FleetOps actions are open."
    return "FleetOps shift brief: review current monitoring status and open work."


@app.get("/api/daily-ops-brief")
def api_daily_ops_brief():
    """Return a shift-ready FleetOps brief combining health, dispatch, and focus items."""
    summary = api_summary()
    dispatch = api_dispatch_summary()
    priority_sites = dispatch["priority_sites"][:3]
    status = dispatch["status"] if dispatch["status"] != "green" else summary["overall_status"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "headline": _daily_ops_headline(
            status,
            dispatch["immediate_action_count"],
            dispatch["scheduled_action_count"],
            dispatch["monitoring_gap_count"],
            dispatch["top_site"],
        ),
        "site_count": summary["sites"],
        "average_score": summary["average_score"],
        "critical_alerts": summary["critical_alerts"],
        "open_action_count": dispatch["open_action_count"],
        "immediate_action_count": dispatch["immediate_action_count"],
        "scheduled_action_count": dispatch["scheduled_action_count"],
        "missing_snapshot_count": dispatch["missing_snapshot_count"],
        "stale_snapshot_count": dispatch["stale_snapshot_count"],
        "monitoring_gap_count": dispatch["monitoring_gap_count"],
        "next_queue": dispatch["next_queue"],
        "top_client": dispatch["top_client"],
        "top_site": dispatch["top_site"],
        "recommended_focus": dispatch["top_action"],
        "priority_sites": priority_sites,
        "monitoring_sites": dispatch["monitoring_sites"][:3],
    }


def _account_agenda_focus(review: dict) -> str:
    """Return the primary agenda theme for an account follow-up."""
    if review["immediate_action_count"]:
        return "incident response"
    if review["scheduled_action_count"]:
        return "maintenance planning"
    if review["monitoring_gap_count"]:
        return "monitoring restoration"
    if review["average_score"] < 85:
        return "health improvement"
    return "routine review"


@app.get("/api/account-agenda")
def api_account_agenda(limit: int = 10):
    """Return a bounded account agenda for weekly service planning."""
    bounded_limit = max(1, min(limit, 50))
    priority_rank = {"urgent": 0, "scheduled": 1, "routine": 2}
    agenda = []
    for review in _client_service_review_rows():
        agenda.append(
            {
                "client": review["client"],
                "priority": review["review_priority"],
                "status": review["status"],
                "focus": _account_agenda_focus(review),
                "site_count": review["site_count"],
                "current_snapshot_count": review["current_snapshot_count"],
                "missing_snapshot_count": review["missing_snapshot_count"],
                "stale_snapshot_count": review["stale_snapshot_count"],
                "monitoring_gap_count": review["monitoring_gap_count"],
                "open_action_count": review["open_action_count"],
                "immediate_action_count": review["immediate_action_count"],
                "scheduled_action_count": review["scheduled_action_count"],
                "top_site": review["top_site"],
                "talking_point": review["talking_point"],
                "next_action": review["next_action"],
                "latest_snapshot_at": review["latest_snapshot_at"],
            }
        )
    agenda.sort(
        key=lambda item: (
            priority_rank.get(item["priority"], 99),
            -item["open_action_count"],
            -item["monitoring_gap_count"],
            item["client"].lower(),
        )
    )
    selected = agenda[:bounded_limit]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": bounded_limit,
        "account_count": len(agenda),
        "returned_account_count": len(selected),
        "urgent_count": sum(1 for item in agenda if item["priority"] == "urgent"),
        "scheduled_count": sum(1 for item in agenda if item["priority"] == "scheduled"),
        "routine_count": sum(1 for item in agenda if item["priority"] == "routine"),
        "monitoring_gap_account_count": sum(1 for item in agenda if item["monitoring_gap_count"]),
        "monitoring_gap_count": sum(item["monitoring_gap_count"] for item in agenda),
        "agenda": selected,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    now = datetime.now(timezone.utc)
    fleet_rows = store.latest_dashboard()
    for row in fleet_rows:
        freshness, age_hours = _snapshot_freshness(
            row.get("captured_at"),
            now,
            SNAPSHOT_FRESHNESS_HOURS,
        )
        row["snapshot_freshness"] = freshness
        row["snapshot_age_hours"] = age_hours
    care_checks = store.latest_care_checks()
    for check in care_checks:
        freshness, age_hours = _snapshot_freshness(
            check.get("checked_at"),
            now,
            SNAPSHOT_FRESHNESS_HOURS,
        )
        check["check_freshness"] = freshness
        check["check_age_hours"] = age_hours
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "fleet_rows": fleet_rows,
            "care_checks": care_checks,
            "sites": store.list_sites(),
            "fleet_summary": api_summary(),
        },
    )


@app.post("/sites")
def add_site(name: str = Form(...), url: str = Form(...), client: str = Form("")):
    store.upsert_site(name, url, client)
    return RedirectResponse("/", status_code=303)


@app.delete("/sites")
def delete_site(url: str):
    """Remove a site and all its care checks and fleet snapshots.

    The URL is normalized using the same rules as site registration so callers
    do not need to spell the canonical form exactly.  Returns 404 when no
    matching site exists.
    """
    normalized = normalize_site_url(url)
    deleted = store.delete_site(normalized)
    if not deleted:
        return JSONResponse(
            status_code=404,
            content={"deleted": False, "url": normalized, "detail": "Site not found."},
        )
    return {"deleted": True, "url": normalized}


@app.post("/care/manual-check")
def manual_care_check(
    name: str = Form(..., min_length=1),
    url: str = Form(..., min_length=1),
    client: str = Form(""),
    http_status: int = Form(200, ge=100, le=599),
    latency_ms: int = Form(250, ge=0, le=SQLITE_INTEGER_MAX),
    ssl_days_remaining: int = Form(60, ge=0, le=SQLITE_INTEGER_MAX),
    wordpress_version: str = Form("unknown"),
    update_count: int = Form(0, ge=0, le=SQLITE_INTEGER_MAX),
    backup_age_hours: int = Form(24, ge=0, le=SQLITE_INTEGER_MAX),
):
    name = normalize_site_name(name)
    url = normalize_site_url(url)
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
    name = normalize_site_name(name)
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


def _security_headers_from_count(security_header_count: int) -> dict[str, str]:
    """Represent count-only snapshots in care reports without losing coverage."""
    monitored_headers = (
        "strict-transport-security",
        "x-frame-options",
        "content-security-policy",
    )
    bounded_count = max(0, min(security_header_count, len(monitored_headers)))
    return {header: "reported present" for header in monitored_headers[:bounded_count]}


@app.post("/snapshot")
def snapshot(
    name: str = Form(..., min_length=1),
    url: str = Form(..., min_length=1),
    client: str = Form(""),
    uptime_ok: bool = Form(True),
    ssl_days: int = Form(60, ge=0, le=SQLITE_INTEGER_MAX),
    wp_updates: int = Form(0, ge=0, le=SQLITE_INTEGER_MAX),
    backup_age_hours: int = Form(24, ge=0, le=SQLITE_INTEGER_MAX),
    response_ms: int = Form(250, ge=0, le=SQLITE_INTEGER_MAX),
    security_header_count: int = Form(3, ge=0, le=3),
):
    name = normalize_site_name(name)
    url = normalize_site_url(url)
    site = FleetSite(name, url, uptime_ok, ssl_days, wp_updates, backup_age_hours, response_ms, security_header_count)
    site_id = store.upsert_site(name, url, client)
    store.save_snapshot(site_id, site, calculate_health_score(site), generate_alerts(site))
    check = evaluate_site(
        name,
        url,
        200 if uptime_ok else 0,
        response_ms,
        ssl_days,
        "unknown",
        wp_updates,
        backup_age_hours,
        _security_headers_from_count(security_header_count),
    )
    store.save_care_check(site_id, check)
    return RedirectResponse("/", status_code=303)


def _combined_report_status(
    care_checks: list[SiteCheck],
    fleet_rows: list[dict],
    monitoring_gap_count: int,
) -> str:
    """Return the highest current risk represented by the combined report."""
    if any(check.status == "red" for check in care_checks) or any(
        _dashboard_status(row["score"]) == "red"
        or any(alert.get("severity") == "critical" for alert in row["alerts"])
        for row in fleet_rows
    ):
        return "red"
    if monitoring_gap_count or any(check.status == "yellow" for check in care_checks) or any(
        _dashboard_status(row["score"]) == "yellow"
        or any(alert.get("severity") == "warning" for alert in row["alerts"])
        for row in fleet_rows
    ):
        return "yellow"
    return "green"


def _build_text_report() -> tuple[str, str, int, int, int, int]:
    """Build a report only from sites with current paired monitoring evidence."""
    now = datetime.now(timezone.utc)
    tracked_sites = store.list_sites()
    latest_snapshots = store.latest_dashboard()
    latest_care_checks = store.latest_care_checks()
    snapshots_by_url = {row["url"]: row for row in latest_snapshots}
    care_checks_by_url = {row["url"]: row for row in latest_care_checks}
    current_urls: set[str] = set()
    evidence_gaps = []
    for site in tracked_sites:
        snapshot = snapshots_by_url.get(site["url"])
        care_check = care_checks_by_url.get(site["url"])
        snapshot_freshness = (
            _snapshot_freshness(
                snapshot.get("captured_at"),
                now,
                SNAPSHOT_FRESHNESS_HOURS,
            )[0]
            if snapshot
            else "missing"
        )
        care_check_freshness = (
            _snapshot_freshness(
                care_check.get("checked_at"),
                now,
                SNAPSHOT_FRESHNESS_HOURS,
            )[0]
            if care_check
            else "missing"
        )
        if snapshot_freshness == "current" and care_check_freshness == "current":
            current_urls.add(site["url"])
            continue
        evidence_gaps.append(
            {
                "name": site["name"],
                "client": site.get("client") or "Unassigned",
                "snapshot_freshness": snapshot_freshness,
                "care_check_freshness": care_check_freshness,
            }
        )

    care_checks = [
        SiteCheck(
            name=r["name"],
            url=r["url"],
            http_status=r["http_status"],
            latency_ms=r["latency_ms"],
            ssl_days_remaining=r["ssl_days_remaining"],
            wordpress_version=r["wordpress_version"],
            update_count=r["update_count"],
            backup_age_hours=r["backup_age_hours"],
            security_headers=r["security_headers"],
            score=r["score"],
            status=r["status"],
            summary=r["summary"],
            actions=r["actions"],
            checked_at=r["checked_at"],
        )
        for r in latest_care_checks
        if r["url"] in current_urls
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
        for r in latest_snapshots
        if r["url"] in current_urls
    ]
    report_text = summarize_care_report(care_checks) + "\n---\n\n" + generate_maintenance_report(fleet_sites)
    if evidence_gaps:
        evidence_gaps.sort(key=lambda row: (row["client"].lower(), row["name"].lower()))
        gap_lines = [
            "# Monitoring Evidence Gaps",
            "",
            "The following sites are excluded from current health claims until both monitoring sources are refreshed:",
            "",
        ]
        for gap in evidence_gaps:
            gap_lines.append(
                f"- {gap['name']} ({gap['client']}): "
                f"fleet snapshot {gap['snapshot_freshness']}; "
                f"care check {gap['care_check_freshness']}. "
                "Capture a fresh combined check before publishing site health."
            )
        report_text += "\n---\n\n" + "\n".join(gap_lines) + "\n"
    current_fleet_rows = [row for row in latest_snapshots if row["url"] in current_urls]
    report_status = _combined_report_status(care_checks, current_fleet_rows, len(evidence_gaps))
    return (
        report_text,
        report_status,
        len(care_checks),
        len(fleet_sites),
        len(tracked_sites),
        len(evidence_gaps),
    )


@app.get("/api/report")
def api_report():
    """Return the combined care/fleet report with metadata for integrations."""
    (
        report_text,
        report_status,
        care_check_count,
        site_count,
        tracked_site_count,
        monitoring_gap_count,
    ) = _build_text_report()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": report_status,
        "snapshot_freshness_threshold_hours": SNAPSHOT_FRESHNESS_HOURS,
        "tracked_site_count": tracked_site_count,
        "site_count": site_count,
        "care_check_count": care_check_count,
        "current_evidence_count": site_count,
        "monitoring_gap_count": monitoring_gap_count,
        "line_count": len(report_text.splitlines()),
        "report": report_text,
    }


@app.get("/report", response_class=PlainTextResponse)
def report():
    report_text, _, _, _, _, _ = _build_text_report()
    return report_text


def run():
    import uvicorn

    uvicorn.run("wp_fleet_ops.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
