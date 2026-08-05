# WP FleetOps

WP FleetOps is a combined WordPress client-care and fleet-operations dashboard. It merges the previous WP CarePulse client reporting workflow with WP FleetOps operational health snapshots into one FastAPI application.

## Features

- Site/client registry with SQLite persistence.
- Client care health checks for HTTP status, latency, SSL days remaining, WordPress updates, backup age, and security headers.
- Fleet operations snapshots for uptime, SSL, pending updates, backup freshness, response time, security headers, and alerts.
- Fail-closed client account rollups at `/api/clients`, excluding stale telemetry from current scores, health counts, and critical-alert totals while retaining monitoring-gap visibility.
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
- Fail-closed maintenance views at `/api/maintenance-windows` and `/api/maintenance-calendar`, scheduling only work backed by current snapshots while reporting monitoring gaps.
- Fail-closed fleet objectives at `/api/slo`, counting only current snapshots while treating missing or stale telemetry as objective misses.
- Fail-closed combined reports at `/report` and `/api/report`, publishing health claims only for sites with current paired care-check and fleet-snapshot evidence and listing monitoring gaps separately.
- Combined dashboard at `/` and Markdown reports at `/report`.
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
