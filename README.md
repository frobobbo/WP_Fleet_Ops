# WP FleetOps

WP FleetOps is a combined WordPress client-care and fleet-operations dashboard. It merges the previous WP CarePulse client reporting workflow with WP FleetOps operational health snapshots into one FastAPI application.

## Features

- Site/client registry with SQLite persistence; Unicode path/query text is converted to UTF-8 percent escapes and equivalent escape hex casing is canonicalized to prevent duplicate site identities while keeping request URIs fetchable. Sites can be removed with a `DELETE /sites?url=<url>` call that cascades to all care checks and fleet snapshots.
- Bounded site identifiers (200-character site/client labels and 2,048-character URLs) plus normalized 100-character WordPress version values reject accidental oversized submissions before they can inflate persistent dashboard and report data; account-filtered coverage and history APIs enforce the same client-label boundary. Version labels are trimmed, blank values use the `unknown` sentinel, and non-printing site/client/version labels and URL characters are rejected. Malformed DNS hosts, malformed percent escapes, percent-encoded controls, empty ports, ambiguous raw-backslash URLs, and legacy numeric IPv4 URLs are also rejected, semicolon path parameters are preserved, and equivalent expanded and compressed IPv6 host spellings are canonicalized to one tracked endpoint.
- Client care health checks for HTTP status, latency, SSL days remaining, WordPress updates, backup age, and security headers, with domain-level telemetry validation for both HTTP forms and direct integrations.
- Fleet operations snapshots for uptime, SSL, pending updates, backup freshness, response time, security headers, and alerts; impossible negative metrics and out-of-range header counts are rejected before scoring or persistence.
- Atomic combined evidence capture: manual, fetched, and snapshot submissions commit the site, fleet snapshot, and paired care check together, preventing partial observations when persistence fails.
- Fail-closed dashboard and client account rollups at `/api/summary` and `/api/clients`, excluding stale fleet snapshots and care checks from current scores, risk counts, health counts, and critical-alert totals while retaining monitoring-gap visibility. The summary reports paired current-evidence count and coverage alongside independent source freshness, reports stale, invalid-timestamp, and clock-skew evidence separately for both observation types, returns a null current average when no paired current evidence exists while retaining a labeled observed average, and excludes invalid or future-dated timestamps from its latest-snapshot marker.
- Fail-closed site inventories at `/api/sites` and `/api/site-directory`, reporting stale, invalid, or future-dated snapshot health as unknown while retaining explicitly labeled observed status for investigation.
- Paired monitoring coverage at `/api/monitoring-coverage`, reporting per-site fleet snapshot and care-check freshness plus the minimum evidence capture needed to restore coverage, so integrations can close gaps without rerunning evidence that is already current; dispatch, daily brief, client update, service review, follow-up, and account agenda APIs route either evidence gap to operators. Use `?client=<name>` (or `Unassigned`) for an account-scoped audit, with unknown accounts returning `404` instead of an empty healthy result.
- Fail-closed site scorecards at `/api/site-scorecards`, including tracked sites missing their first snapshot and replacing stale scores, status, badges, and alert counts with unknown values while retaining labeled observations for investigation.
- Fail-closed operator watchlist at `/api/site-watchlist`, keeping stale alerts out of the current work queue while surfacing stale and missing snapshot evidence as monitoring gaps.
- Fail-closed prioritized action queue at `/api/actions`, reporting stale and missing snapshot evidence so an empty current queue cannot be mistaken for complete fleet health.
- Fail-closed critical incident feed at `/api/incidents`, keeping stale alerts out of current escalations while surfacing missing or stale paired fleet-snapshot and care-check evidence.
- Fail-closed client workload at `/api/client-workload`, keeping current fleet actions separate from account-grouped paired snapshot and care-check evidence gaps.
- Fail-closed action matrix at `/api/action-matrix`, grouping current dispatch work by client and site while listing paired monitoring gaps separately.
- Paired-evidence management KPIs at `/api/operations-kpis`, warning when either fleet snapshots or care checks are incomplete and recommending the next evidence-restoration step.
- Fail-closed remediation plan at `/api/remediation-plan`, routing missing, stale, invalid, and future-dated snapshot or care-check evidence into a dedicated monitoring bucket instead of returning an empty operator plan.
- Coverage-aware site and client priorities at `/api/site-priorities` and `/api/client-priorities`, excluding stale risk signals while reporting tracked, missing, stale, and current snapshot counts so an empty dispatch queue cannot be mistaken for complete evidence.
- Fail-closed site trends at `/api/site-trends`, including tracked sites missing their first snapshot and suppressing stale score deltas and trend labels while retaining labeled observations for investigation.
- Fail-closed client digests at `/api/client-digest`, suppressing nested site health claims when either the fleet snapshot or paired care check is stale or missing, while retaining labeled observations and surfacing account-level evidence-restoration steps.
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
- Fail-closed executive account risk rollups at `/api/executive-risks`, `/api/fleet-brief`, and `/api/operator-handoff`, excluding stale critical scores and incidents while escalating incomplete paired fleet-snapshot and care-check evidence.
- Fail-closed maintenance views at `/api/maintenance-windows` and `/api/maintenance-calendar`, scheduling only work backed by current snapshots while reporting monitoring gaps.
- Fail-closed fleet objectives at `/api/slo`, counting only current snapshots while treating missing or stale telemetry as objective misses.
- Fail-closed combined reports at `/report` and `/api/report`, publishing health claims only for sites with current paired care-check and fleet-snapshot evidence, listing monitoring gaps separately, and exposing the highest current risk in the structured report status.
- Fail-closed combined dashboard at `/`, excluding stale fleet and care observations from headline health metrics while retaining clearly labeled observed values for investigation, plus Markdown reports at `/report`.
- Self-contained dashboard assets and a restrictive Content Security Policy avoid third-party font/CDN requests from the internal operations UI.
- Conditional gzip compression reduces transfer size for growing fleet inventories and reports while leaving small health probes uncompressed.
- Readiness checks at `/ready` verify both SQLite reads and a rolled-back no-op write transaction, preventing read-only or mis-mounted data volumes from receiving traffic; database-lock waits are bounded below the Kubernetes probe timeout so abandoned checks cannot accumulate blocked worker threads.
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
- `WP_FLEET_OPS_REVISION`: deployed Git revision reported by `/health` and `/ready`; defaults to `unknown`.
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

The image health check uses `/ready`, so container runtimes mark the service
unhealthy when its SQLite data volume cannot be read and written.

## Helm

```bash
helm upgrade --install wp-fleet-ops ./charts/wp-fleet-ops \
  --namespace wp-fleet-ops --create-namespace \
  --set image.repository=ghcr.io/frobobbo/wp_fleet_ops \
  --set image.tag=latest \
  --set-string config.revision="$(git rev-parse HEAD)"
kubectl -n wp-fleet-ops port-forward svc/wp-fleet-ops 8080:80
```

The chart defaults to `imagePullPolicy: Always` because `latest` is mutable;
this prevents a rollout from silently reusing an older node-cached image. For
immutable digest or `sha-*` tags, operators may override the policy to
`IfNotPresent`. The Deployment retains three ReplicaSet revisions by default,
providing short rollback history without accumulating the Kubernetes default
of ten revisions during frequent maintenance rollouts. Set
`revisionHistoryLimit` to tune that retention. A bounded startup probe allows
up to three minutes for image initialization or source-bundle dependency
installation and requires SQLite readiness before liveness checks begin.
The normal readiness probe allows two seconds for FleetOps' one-second bounded
SQLite lock wait to return a controlled response during brief contention.
The Deployment also requires five seconds of sustained readiness before a new
Pod is counted as available, so a single transient probe cannot prematurely
complete a Helm rollout. Set `minReadySeconds` to tune that stabilization
window.
The default Pod security context uses `fsGroupChangePolicy: OnRootMismatch`, so
the retained SQLite volume is recursively relabeled only when its root ownership
does not match the configured group instead of on every maintenance rollout.
Service-link environment injection is disabled by default because FleetOps uses
DNS-based Service discovery and does not need ambient cluster Service metadata.
The container security context also pins non-root UID `100` and primary GID
`1000` for both the application image and stock-Python source fallback, avoiding
an image-dependent root primary group while retaining PVC access through the
matching filesystem group.

Then open http://127.0.0.1:8080/health or http://127.0.0.1:8080/.

The chart retains its SQLite PVC on `helm uninstall` by default, preventing
release removal from silently deleting FleetOps history. To opt into destructive
PVC cleanup for an intentionally disposable installation, set
`persistence.keep=false` before uninstalling the release.

### Source-bundle fallback

When the application image is unavailable, build the fallback archive with the
allowlisted helper rather than archiving the repository root:

```bash
python scripts/build_source_bundle.py
kubectl -n wp-fleet-ops create configmap wp-fleet-ops-source \
  --from-file=app.tar.gz=source-bundle.tar.gz \
  --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install wp-fleet-ops ./charts/wp-fleet-ops \
  --namespace wp-fleet-ops \
  --set sourceBundle.enabled=true \
  --set sourceBundle.configMapName=wp-fleet-ops-source
```

The builder includes only `pyproject.toml`, `uv.lock`, `wp_fleet_ops/`, and
`templates/`; rejects symlinks; omits caches; and writes the archive atomically
with mode `0600`. It fails before deployment if the compressed archive exceeds
the conservative ConfigMap size limit. If the ConfigMap uses a different archive
key, set `sourceBundle.fileName` to that key; the chart projects it to the stable
`/bundle/app.tar.gz` runtime path expected by the init container. The unpacked
source and installed dependencies use an ephemeral volume capped at 512 MiB by
default; tune `sourceBundle.workSizeLimit` if a future dependency set needs more
space rather than leaving node-local storage unbounded.
