# Data Storage and Privacy

EZ Mark is local-first: no cloud account, no telemetry, and no external
service is needed for Standard Test or Peer Evaluation workflows. Nothing is
uploaded anywhere automatically.

## Data locations by mode

Everything under the data root is standard: plain SQLite for records, plain
files for documents, no obfuscation. All database references to files are
stored relative to the data root.

| Item | Development (`python` / `uvicorn`) | Packaged Windows app | Demo mode (`DEMO_MODE=1`) |
| --- | --- | --- | --- |
| Data root | `data/` in the repo | `%LOCALAPPDATA%\EZMark\data` (or `EZMARK_ROOT\data` for portable installs) | `data-demo/` (or `DEMO_DATA_ROOT`) |
| SQLite database | `data/app.sqlite` | data root / `app.sqlite` | demo root / `app.sqlite` |
| Uploaded scans | `data/uploaded_scans/` | data root / `uploaded_scans/` | demo root / `uploaded_scans/` |
| Processed pages | `data/processed_pages/` | same layout | demo root |
| Generated PDFs | `data/generated_pdfs/` | same layout | demo root |
| Review crops | `data/review_crops/` | same layout | demo root |
| Exports (XLSX/CSV) | `data/exports/` | same layout | demo root |
| Package imports/exports | `data/import_previews/`, `data/package_exports/` | same layout | demo root |
| Logs | console (`LOG_PATH` env / launcher) | `%LOCALAPPDATA%\EZMark\logs` | console or data-root-adjacent logs |
| Configuration | `.env` in the repo | `%LOCALAPPDATA%\EZMark\config\local.env` | same env mechanism, `DEMO_*` vars |
| Backups | `data/`-external `backups/` (launcher-managed, when supported) | `%LOCALAPPDATA%\EZMark\backups` | demo wipe instead |

Separation of concerns: application binaries/assets (the install/repo
directory: exe, `app/templates`, `app/static`, `app/locales`), persistent
data (the data root), logs, and configuration are always separate directories;
portable installs keep data/logs/config beside the executable, packaged
installs do not require the install directory to be writable.

## Overrides

- `DATA_ROOT` — data root directory (wins over every default, in every mode).
- `DATABASE_URL` — full database URL override.
- `EZMARK_ROOT` — deliberate portable packaged install: data/logs/config live
  beside the executable instead of `%LOCALAPPDATA%\EZMark`.
- `DEMO_DATA_ROOT` — demo-only data root (default `data-demo/`).

## Scans and PII

Answer sheets contain student names and numbers as printed content; they stay
in the local data root only. Generated QR codes on sheets never contain PII —
only random, database-trackable opaque tokens.

## Cleanup

Deleting a run's scans/archives removes the files from the data root.
Uninstalling packaged EZ Mark leaves data, logs and configuration under
`%LOCALAPPDATA%\EZMark`; deleting that directory removes all user data.
Demo data lives entirely under its demo root and is wiped/reseeded by
`python scripts/seed_demo.py --reset` or `DEMO_RESET_ON_START=1`.
