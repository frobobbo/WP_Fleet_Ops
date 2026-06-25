# WP FleetOps 6-hour maintenance prompt

Schedule: every 6 hours (`0 */6 * * *`).

Prompt:

```text
Work on WP FleetOps in https://github.com/frobobbo/WP_Fleet_Ops. Pull the latest main branch, inspect open issues/TODOs and the current app/chart/CI state, then create or tweak one useful feature for the combined WordPress client-care and fleet-operations dashboard. Run tests, update docs if needed, build/lint container or Helm artifacts when relevant, commit with a conventional commit, push or open a PR, and summarize what changed. Do not expose secrets. If deployment access is available, redeploy the Helm release in namespace wp-fleet-ops and verify /health.
```

Leaf subagents cannot create Hermes cron entries directly; this file is the source prompt for the parent agent to install/update the recurring task.
