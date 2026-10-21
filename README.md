# EZ Mark

**English** | [日本語](README.ja.md)

## OMR Scanner App

Create, print, scan and review multiple-choice answer sheets with automatic QR matching and OMR recognition.

EZ Mark is a local-first web application for Standard Tests (multiple-choice, raw-score) and Peer Evaluation (anonymous rating forms). Import a student roster, design a reusable template, generate printable PDF mark sheets with QR codes, scan the collected sheets (PDF/JPG/JPEG/PNG), fix only the unclear marks in a review queue, and get scores, percentages, pass/fail, question analysis, and XLSX/CSV exports.

## Features

- **Test Templates**: reusable setups (questions, answer key, points, scoring sections) that create separate test runs.
- **Standard Tests**: multiple-choice answer sheets scored with raw points; optional pass marks by points or percentage.
- **Peer Evaluation**: anonymous rating forms where participants rate each other (1–5 scale) per competency.
- **Student roster import** from XLSX or CSV with loud validation, preview, and duplicate/conflict resolution.
- **PDF mark sheet generation** with:
  - printed student name/number and alignment markers;
  - QR codes containing only random, database-trackable opaque tokens (never personal data);
  - bubble coordinates stored at PDF generation time so scans are read against the exact layout.
- **Scan upload** (PDF/JPG/JPEG/PNG) with automatic QR matching, plus blank spare sheets that can be assigned to a student during upload.
- **Review workflow**: only unclear, blank, multiple-mark, or unmatched bubbles surface in the review queue; bulk page actions and hotkeys supported.
- **Scoring**: raw points, percentages, explicit pass marks, group averages, and question-level analysis (correct rates, blanks, multiple marks, review priorities).
- **Exports**: student summary, per-question answers, and question analysis as XLSX or CSV.
- **Peer Evaluation results**: overall ranking, group comparison, competency averages, per-target ratings.
- **Japanese/English UI** (switchable per user).
- **Demo mode** with fully synthetic seed data and uploads/disabled imports, separate `data-demo/` storage.
- **Local-first**: SQLite database, files on disk, no cloud account, no telemetry.

## Screenshots

_Screenshots to be added._ The app uses a server-rendered Jinja2 + HTMX UI with a light/dark visual theme.

## Architecture overview

- **Backend**: Python 3 with FastAPI; server-rendered Jinja2 templates with HTMX for dynamic updates.
- **Database**: SQLite via SQLAlchemy with schema versioning (a `schema_info` version marker; incompatible legacy databases are refused instead of silently upgraded).
- **OMR pipeline**: OpenCV + NumPy for bubble detection and alignment; `qrcode` + Pillow for QR generation; ReportLab for A4 PDF generation; pypdfium2 for rasterizing uploaded PDF scans.
- **i18n**: JSON locale dictionaries under `app/locales/` (en/ja) served through a small i18n service.
- **Storage**: everything lives under a configured data root (`data/` for development; `%LOCALAPPDATA%\EZMark` or `EZMARK_ROOT` for packaged installs; `data-demo/` for demo mode). Database records reference files by relative paths.

The app generates its own controlled mark sheets instead of being a general-purpose OMR template editor. The exact bubble coordinate map is saved when the PDF is generated, and the scan reader reads those same coordinates — which keeps recognition simple and reliable.

## Requirements

- Python 3.10+ (tested with Python 3.10–3.13 on Windows; other platforms/versions are untested)
- A modern browser (Chrome is used for the packaged Windows app mode)
- No database server, no external cloud services

## Installation

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate
pip install -r requirements.txt
```

See [docs/installation.md](docs/installation.md) for details and packaged Windows installs.

## Running

Development (auto-reload web server):

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Local production-style server (no reload; reads `HOST`, `PORT`, `LOG_LEVEL`):

```bash
python scripts/run_ezmark_server.py
```

Then open `http://127.0.0.1:8000`.

### Demo mode

```bash
set DEMO_MODE=1            # (export DEMO_MODE=1 on macOS/Linux)
python scripts/seed_demo.py --reset
python scripts/run_ezmark_server.py
```

Demo mode uses `data-demo/` storage, seeds a synthetic 20-question quiz and a peer evaluation example with fictional names, and disables uploads/imports. See [docs/demo-mode.md](docs/demo-mode.md).

## Local-first privacy model

- EZ Mark runs on your machine; scans, rosters, scores, and exports never leave your data root.
- Nothing is sent to any external service; there is no account and no telemetry.
- QR codes on printed sheets contain random opaque tokens only — no names or student numbers.
- The hosted variant exists for shared demo purposes only; personal-data upload endpoints are disabled there.
- See [docs/data-storage-and-privacy.md](docs/data-storage-and-privacy.md) for exact data locations and cleanup.

## Data locations

| Mode | Data root |
| --- | --- |
| Development | `data/` in the project directory (override: `DATA_ROOT`/`DATABASE_URL`) |
| Packaged Windows app | `%LOCALAPPDATA%\EZMark\data` (portable installs: `EZMARK_ROOT` location) |
| Demo mode | `data-demo/` (override: `DEMO_DATA_ROOT`) |

## Standard Test workflow

1. **Import a roster** — paste students or upload XLSX/CSV (`student_number`, `name`, `group`).
2. **Create a template** — name it and open the builder; define question structures (continuous or grouped numbering), choices per question, points, and optional scoring sections.
3. **Set the answer key** — pick correct answers per question; answers are also printable as a key sheet.
4. **Create a test run** from the template, add/import its roster, and **generate sheets** — pre-filled PDFs (plus optional spare blank sheets) with QR tokens.
5. **Print and scan** — print at 100% scale; upload scans as PDF/JPG/JPEG/PNG files; QR matching happens automatically and unmatched/blank pages can be assigned afterwards.
6. **Review** — only unclear marks need attention; confirm or correct each one (or apply bulk page decisions).
7. **Results** — calculate raw scores, percentages, and pass/fail; inspect group averages and question analysis.
8. **Export** — download the student summary, per-question answer dump, or question analysis as XLSX/CSV.

Each step of run setup supports XLSX/CSV roster import: required headers are validated and missing columns, duplicate student numbers, and conflicting rows fail loudly with a preview screen rather than silently importing.

## Supported scan formats

- **PDF** (rasterized internally with pypdfium2 at 300 DPI)
- **JPG / JPEG / PNG** images

Camera scans are not a supported input; use a scanner (300 DPI recommended).

## QR mapping

Each generated sheet gets a random opaque token encoded in its QR code and stored in the database. Uploaded scans are decoded and matched against these tokens, so sheets map to students without any personal data being printed inside the QR code.

## Scoring, pass marks and question analysis

- Every question has a **points** value; totals are the exact sum (fractional points allowed and preserved).
- **Pass marks** are optional: by raw points or by percentage. Without one, results get a neutral "Scored" status.
- **Question analysis** lists per-question correct rates, blanks, and multiple-mark counts, and highlights questions worth reviewing first.
- Scores are reported as raw scores/percentages — this is an OMR scoring tool, not a standardized-test scaled-score engine.

## Peer Evaluation

Participants rate one another anonymously on a 1–5 scale per **competency** (skill). Templates define competencies, rating scale, rater groups, and students-per-page layout; each session gets its own roster, rating form PDF, scan upload, review queue, and results (overall ranking, group comparison, competency averages). Review again covers only unclear/blank/invalid ratings.

See [docs/peer-evaluation.md](docs/peer-evaluation.md).

## Demo mode

A dedicated synthetic demo dataset for shared/public instances: `DEMO_MODE=1`, `data-demo/` storage, seeding via `scripts/seed_demo.py`, uploads and imports disabled. See [docs/demo-mode.md](docs/demo-mode.md).

## Hosted deployment

The FastAPI app can be hosted (e.g. `HOST=0.0.0.0 PORT=8000 python scripts/run_ezmark_server.py`). SQLite is single-writer: run **one worker process** only. For shared demos prefer demo mode and persist the data root. See [docs/hosted-deployment.md](docs/hosted-deployment.md).

## Windows packaging status

Windows is the tested platform. PyInstaller packaging builds (`ezmark-server.exe` server package and the `EZMark` desktop launcher) live under `packaging/windows/` — see [docs/windows-packaging.md](docs/windows-packaging.md) and [packaging/windows/README.md](packaging/windows/README.md).

## Limitations

An honest, current list — see [docs/limitations.md](docs/limitations.md). Highlights:

- Not a general-purpose OMR template designer; sheets must be generated by EZ Mark.
- No handwriting/reading of names, no OCR, no machine-learning mark detection.
- No cloud sync, no user accounts/roles (single-operator tool), no official standardized-test scoring.
- Japanese PDF output depends on system fonts (no fonts are bundled).
- SQLite is single-writer; hosted deployments are limited to one worker.

## Testing

```bash
venv\Scripts\python.exe -m pytest
venv\Scripts\python.exe -m ruff check .
venv\Scripts\python.exe -m ruff format --check .
```

The test suite covers templates, scoring, review, peer evaluation, imports/exports, demo mode, packaging helpers, and localization. See [docs/development-and-testing.md](docs/development-and-testing.md).

## License

MIT — see [LICENSE](LICENSE); third-party notices in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
- Security: [SECURITY.md](SECURITY.md)
