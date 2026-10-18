# Agent Guide

Guide for AI coding agents working on EZ Mark, a local-first OMR scanner web app.

## Project summary

**EZ Mark** is a standalone local-first web application for Standard Tests (raw-score multiple-choice OMR) and Peer Evaluation (anonymous rating forms). It manages the full workflow: importing student/participant rosters, configuring question structures and answer keys, printing pre-filled PDF mark sheets with mapping QR codes, scanning/reading bubble responses via OpenCV, reviewing exceptions in an unclear-mark queue, calculating raw scores/percentages/pass-fail, and exporting XLSX/CSV reports.

Avoid describing the product in terms of any specific institution type; keep copy generic (education, training, workshops, organizations).

## Product modes

- `generic_test` — **Standard Test**: multiple-choice, raw points, optional pass marks (points or percentage).
- `peer_review` — **Peer Evaluation**: anonymous 1–5 competency ratings between participants.

## Terminology rules

- Visible wording uses **Group**, not **Class** (internal `class_name`/`group_name` fields may keep their names).
- No historical/legacy domain vocabulary (even if the surrounding code still says "exam" in places — e.g. the internal `MockExam` model/table `mock_exams` is a schema-v1-locked name; do not rename it).
- New visible strings must exist in both `app/locales/en.json` and `app/locales/ja.json` (tests check parity). Prefer locale keys over hardcoded template/JS strings (`_("key")` in Jinja, `window.EZ_MARK_I18N` + `t("key", fallback)` in `app/static/js/template-builder.js`).

## Stack

- **Core/Backend**: Python (FastAPI, Jinja2, python-dotenv, HTMX)
- **Database / Versioning**: SQLite via SQLAlchemy; public schema baseline v1 (`schema_info` marker, no Alembic migrations at runtime)
- **PDF Generation**: ReportLab
- **Image / Scan Processing**: OpenCV, NumPy, pypdfium2 (rasterizer), qrcode + Pillow
- **Excel/CSV**: openpyxl, standard `csv` module
- **QA**: pytest, ruff

## Important project invariants

- **PII Protection**: student names, student numbers, and other PII must *never* appear in generated QR codes. Sheets carry random, database-trackable opaque tokens.
- **Controlled Generation**: the app saves the exact bubble coordinate maps at PDF generation time; the scan reader uses those stored maps for the matching sheet version.
- **Local-first**: all data stays in the local data root; no cloud services, accounts, or telemetry.
- **Scoring source of truth**: only final reviewed responses drive scoring; totals are exact (fractional points allowed, never rounded up). Results are raw scores/percentages/pass-fail — never an "official standardized test score".
- **Fail Loudly**: Excel/CSV imports validate required headers and row structures and reject missing columns, duplicate student numbers, and conflict rows loudly via a preview screen.
- **Relative Storage Paths**: file paths stored in SQLite (PDFs, scans, crops, exports) are relative to the data root.
- **Schema v1**: databases must carry the `schema_version = 1` marker; startup refuses incompatible/legacy databases with `IncompatibleSchemaError` instead of migrating in place.
- **Date Format**: date-only values are plain ISO strings (`YYYY-MM-DD`).
- **Synthetic data only**: no real personal data in tests, examples, docs, seeds, or screenshots. Demo mode (`DEMO_MODE`, `DEMO_RESET_ON_START`, `data-demo/`) is fully isolated and disables uploads/imports.

## Commands

Run inside `D:\...\EZ-Mark` with a project venv at `venv/`.

Install:

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Development run:

```bash
venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Local production-style run (no reload; reads `HOST`/`PORT`/`LOG_LEVEL`):

```bash
venv\Scripts\python.exe scripts\run_ezmark_server.py
```

Demo seeding:

```bash
venv\Scripts\python.exe scripts\seed_demo.py [--reset]
```

Test (470+ tests, ~all pass on the migration branch):

```bash
venv\Scripts\python.exe -m pytest
venv\Scripts\python.exe -m pytest tests\<file>::<case> -q
```

Lint/format:

```bash
venv\Scripts\python.exe -m ruff check .
venv\Scripts\python.exe -m ruff format --check .
```

Packaged Windows builds live in `packaging/windows/` (see that README).

## Working rules

- Read existing files before adding new abstractions.
- Make the smallest safe change; do not refactor adjacent code.
- Public schema v1 is frozen; do not remove/rename columns (additive or none).
- If two project patterns conflict, choose the more current/tested one and flag the other.
- Do not report success if checks were skipped.
- Do not use real private data in tests, prompts, docs, screenshots, or examples.

## Documentation rules

Keep public docs accurate: `README.md`, `docs/*` (installation, local-operation, windows-packaging, hosted-deployment, data-storage-and-privacy, standard-tests, peer-evaluation, scanning-and-review, demo-mode, limitations, development-and-testing, architecture), `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`. Update them when changing setup, commands, routes, data model, workflows, storage, or generated-file behavior.
