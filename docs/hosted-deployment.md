# Hosted Deployment

EZ Mark is local-first, and the primary supported way to run it is on one trusted local machine. The same FastAPI app can be hosted on a private server, mainly to serve **shared demo instances** or a small team on a trusted LAN.

## Basic hosting

```bash
HOST=0.0.0.0 PORT=8000 LOG_LEVEL=info python scripts/run_ezmark_server.py
```

Both development runners read `HOST`, `PORT`, and `LOG_LEVEL` from the environment. Keep the app behind TLS-terminating proxy if it is exposed beyond a trusted network — the app itself serves plain HTTP and has **no built-in authentication**: whoever reaches the port can read and modify everything, including rosters and scans.

## SQLite is single-writer

EZ Mark uses SQLite. Run **exactly one application worker process**:

- no `uvicorn --workers N`;
- no multiple replicas/containers sharing one database file;
- no two copies of the app against the same data root.

For higher traffic or team use, plan for one instance per data root (or per-tenant data roots through `DATA_ROOT`).

## Persistent data

Set explicit `DATA_ROOT` / `DATABASE_URL` (or `DEMO_DATA_ROOT`) to persistent storage. Do not rely on container-local ephemeral paths if state should survive restarts. Database files and file folders (scans, generated PDFs) both live under the data root, so a volume mount of that directory captures everything.

## Schema upgrades

There are no automatic migrations. Startup (`app/database.py`) checks the `schema_info` version marker and **refuses** databases with an unexpected or legacy schema (`IncompatibleSchemaError`) instead of modifying them. Upgrading a hosted instance means stopping it, taking a backup of the data root, and letting a matching version create/validate the schema.

## Shared demo instances

Prefer demo mode for anything public-facing (see [demo-mode.md](demo-mode.md)):

- `DEMO_MODE=1` isolates demo data into `data-demo/` (or `DEMO_DATA_ROOT`);
- roster/package/scan uploads are rejected (HTTP 403) so visitors cannot leave personal data;
- seed with `python scripts/seed_demo.py --reset` (or `DEMO_RESET_ON_START=1` for intentionally ephemeral resets on every start);
- still run one worker only.

Do not host a normal (non-demo) instance wide-open: it would hold real rosters and scans behind no authentication.
