from __future__ import annotations

import os
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
    name: str = Form(...),
    url: str = Form(...),
    client: str = Form(""),
    http_status: int = Form(200),
    latency_ms: int = Form(250),
    ssl_days_remaining: int = Form(60),
    wordpress_version: str = Form("unknown"),
    update_count: int = Form(0),
    backup_age_hours: int = Form(24),
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


@app.get("/report", response_class=PlainTextResponse)
def report():
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
    return summarize_care_report(care_checks) + "\n---\n\n" + generate_maintenance_report(fleet_sites)


def run():
    import uvicorn

    uvicorn.run("wp_fleet_ops.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
