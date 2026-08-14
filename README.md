# WP FleetOps

WP FleetOps is a combined WordPress client-care and fleet-operations dashboard. It merges the previous WP CarePulse client reporting workflow with WP FleetOps operational health snapshots into one FastAPI application.

## Features

- Site/client registry with SQLite persistence; sites can be removed with a `DELETE /sites?url=<url>` call that cascades to all care checks and fleet snapshots.
- Client care health checks for HTTP status, latency, SSL days remaining, WordPress updates, backup age, and security headers.
- Fleet operations snapshots for uptime, SSL, pending updates, backup freshness, response time, security headers, and alerts.
- Fail-closed dashboard and client account rollups at `/api/summary` and `/api/clients`, excluding stale fleet snapshots and care checks from current scores, risk counts, health counts, and critical-alert totals while retaining monitoring-gap visibility.
- Fail-closed site inventories at `/api/sites` and `/api/site-directory`, reporting stale, invalid, or future-dated snapshot health as unknown while retaining explicitly labeled observed status for investigation.
- Fail-closed site scorecards at `/api/site-scorecards`, replacing stale scores, status, badges, and alert counts with unknown values while retaining labeled observations for investigation.
- Fail-closed operator watchlist at `/api/site-watchlist`, keeping stale alerts out of the current work queue while surfacing stale and missing snapshot evidence as monitoring gaps.
- Fail-closed prioritized action queue at `/api/actions`, reporting stale and missing snapshot evidence so an empty current queue cannot be mistaken for complete fleet health.
- Fail-closed site trends at `/api/site-trends`, suppressing stale score deltas and trend labels while retaining labeled observations for investigation.
- Fail-closed client digests at `/api/client-digest`, suppressing stale nested site scores, status, and critical-alert totals while retaining labeled observations for investigation.
- Fail-closed availability inventory at `/api/availability`, including missing and stale monitoring evidence.
- Fail-closed SLA breach queue at `/api/sla-breaches`, separating current target misses from missing or stale evidence.
- Fail-closed backup inventory at `/api/backups`, separating current backup age from missing, stale, invalid, or future-dated evidence.
- Fail-closed client backup remediation at `/api/backup-remediation`, keeping current backup risk separate from missing or stale evidence.
- Fail-closed restore-drill queue at `/api/restore-drill-queue`, prioritizing only current backup evidence and surfacing monitoring gaps as unknown.
- Fail-closed certificate inventory at `/api/certificates`, separating current expiry from missing, stale, invalid, or future-dated evidence.
- Fail-closed certificate renewal calendar at `/api/certificate-renewal-calendar`, scheduling only current expiry evidence while surfacing monitoring gaps as unknown.
- Fail-closed update inventory at `/api/updates`, separating current WordPress backlogs from missing, stale, invalid, or future-dated evidence.
- Fail-closed security inventory at `/api/security`, separating current header coverage from missing, stale, invalid, or future-dated evidence.
- Fail-closed performance inventory at `/api/performance`, separating current response times from missing, stale, invalid, or future-dated evidence.
- Fail-closed risk register at `/api/risk-register`, excluding stale observations from current planning risks while surfacing monitoring gaps.
- Fail-closed executive account risk rollups at `/api/executive-risks`, `/api/fleet-brief`, and `/api/operator-handoff`, excluding stale critical scores and incidents while retaining monitoring-gap escalation.
- Fail-closed maintenance views at `/api/maintenance-windows` and `/api/maintenance-calendar`, scheduling only work backed by current snapshots while reporting monitoring gaps.
- Fail-closed fleet objectives at `/api/slo`, counting only current snapshots while treating missing or stale telemetry as objective misses.
- Fail-closed combined reports at `/report` and `/api/report`, publishing health claims only for sites with current paired care-check and fleet-snapshot evidence, listing monitoring gaps separately, and exposing the highest current risk in the structured report status.
- Fail-closed combined dashboard at `/`, excluding stale fleet and care observations from headline health metrics while retaining clearly labeled observed values for investigation, plus Markdown reports at `/report`.
- Container image and Helm chart for Kubernetes deployment.

## Local development

```bash
uv sync --dev
uv run pytest -q
uv run uvicorn wp_fleet_ops.main:app --host 127.0.0.1 --port 8000
```

Environment variables:

- `WP_FLEET_OPS_DATA_DIR`: directory for SQLite data, defaults to `./data` locally and `/data` in the container.
- `WP_FLEET_OPS_DB`: explicit SQLite database path.
- `PORT`: used by the console script runner.

### History API pagination

`/api/snapshot-history`, `/api/care-check-history`, and `/api/site-snapshot-history`
return offset navigation plus `page_number` and `page_count`. Pages are one-based when
results exist; both page fields are `0` for an empty result set. Requested offsets beyond
the result set are clamped to the final page.

## Container

```bash
docker build -t wp-fleet-ops:local .
docker run --rm -p 8000:8000 -v wp-fleet-ops-data:/data wp-fleet-ops:local
```

## Helm

```bash
helm upgrade --install wp-fleet-ops ./charts/wp-fleet-ops \
  --namespace wp-fleet-ops --create-namespace \
  --set image.repository=ghcr.io/frobobbo/wp_fleet_ops \
  --set image.tag=latest
kubectl -n wp-fleet-ops port-forward svc/wp-fleet-ops 8080:80
```

Then open http://127.0.0.1:8080/health or http://127.0.0.1:8080/.
