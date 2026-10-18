# Local Operation

EZ Mark is meant to be operated locally, on one trusted machine, by one person at a time.

## Starting and stopping

- **Development**: `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload` (auto-reload on code changes).
- **Production-style local server**: `python scripts/run_ezmark_server.py` (no reload; reads `HOST`, `PORT`, `LOG_LEVEL`).
- **Packaged Windows**: run `EZMark.exe`; the launcher checks `http://127.0.0.1:8000/healthz`, starts `ezmark-server.exe` if needed (up to 90 s), and opens Chrome in app mode. See [windows-packaging.md](windows-packaging.md).

Stop the server with Ctrl-C (or by closing the launcher). The SQLite database uses WAL mode and closes cleanly.

## Daily workflow

1. Start the app and open the dashboard.
2. Work through test runs: roster import → template/answer key → sheet generation → scanning → review → results → export.
3. Generate PDFs once per sheet version; if you regenerate sheets after scanning started, re-scan the newly printed sheets (regeneration invalidates old QR codes).
4. Archive finished runs: archiving keeps results/insights but deletes the on-disk scan files to save space. Deletion removes everything for that run.

## Printing tips (important for OMR accuracy)

- Print at **100 % scale** — never "fit to page", which rescales coordinates.
- Print in black, reasonably fresh toner/ink.
- Scan at **300 DPI or higher** (flatbed or sheet-fed scanner), portrait orientation as generated.
- Keep the scanner glass/sheet path clean; alignment markers handle small offsets (configurable drift range in settings).

## Data safety during local use

- Back up the data root (`data/` or `%LOCALAPPDATA%\EZMark\data`) regularly; simple file copies are enough because the storage is plain SQLite + files.
- Do not run *two copies* of the app against the same data root at the same time — SQLite is single-writer and concurrent writers can corrupt a database.
- Scans, rosters, and exports contain personal data; the data root is not encrypted. See [data-storage-and-privacy.md](data-storage-and-privacy.md).

## Settings you may tune

The in-app Settings page (scan detection thresholds, print lightness) is the intended tuning surface; values persist in the database. `HOST`/`PORT`/`LOG_LEVEL` via environment variables control the server. There is no other configuration file to maintain.
