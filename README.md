# WP FleetOps

WP FleetOps is a combined WordPress client-care and fleet-operations dashboard. It merges the previous WP CarePulse client reporting workflow with WP FleetOps operational health snapshots into one FastAPI application.

## Features

- Site/client registry with SQLite persistence.
- Client care health checks for HTTP status, latency, SSL days remaining, WordPress updates, backup age, and security headers.
- Fleet operations snapshots for uptime, SSL, pending updates, backup freshness, response time, security headers, and alerts.
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
