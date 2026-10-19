# Development and Testing

How to set up a development environment for EZ Mark and run the test suite.

## Setup

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Runtime dependencies live in `requirements.txt`; the test suite, linter, and
TestClient's `httpx` live in `requirements-dev.txt` (they are not needed to run
the app). Runtime floors are minimal compatibility floors, not pins; when you
bump one, install it locally and run the full suite before committing.

## Running in development

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Then open `http://127.0.0.1:8000`. Data goes to the repo-side `data/` directory (override with `DATA_ROOT`/`DATABASE_URL`).

### Demo dataset for manual testing

```bash
set DEMO_MODE=1                  # Windows cmd; export DEMO_MODE=1 on sh
python scripts/seed_demo.py --reset
python scripts/run_ezmark_server.py
```

This seeds a synthetic 20-question quiz plus a peer evaluation example into `data-demo/`.

## Tests

```bash
python -m pytest
```

- Run the full suite before submitting changes. Review all failures and skips.
- Coverage gaps: no scan images are shipped; scan-processing tests synthesize pages rather than scanning real prints.
- Use dedicated temp data roots (pytest tmp paths), never the repo `data/` root, when a test needs files.

## Lint/format

```bash
python -m ruff check .
python -m ruff format --check .
```

(Ruff config lives in `pyproject.toml`; line length 100, double quotes, LF endings.)

GitHub Actions runs lint, format, compile, and pytest checks on Ubuntu and Windows with Python 3.13.
Tests use synthetic fixtures and temporary data directories. CI does not deploy the app.
These checks do not replace manual Windows package validation.

## Test conventions

- Fully synthetic data only — no real names, numbers, rosters, or scans anywhere in `tests/`.
- TestClient (`with TestClient(create_app()) as client:`) for route-level tests so startup initialization runs.
- Locale keys must exist in both `app/locales/en.json` and `app/locales/ja.json`; wording tests assert visible template copy in both languages where relevant.
- DB-level tests construct SQLAlchemy models directly against a temporary SQLite database.

## Useful internals

- `app/database.py` — engine, schema v1 marker, `IncompatibleSchemaError`.
- `app/services/` — the whole OMR/scoring/peer-review logic; prefer service-level tests over UI tests.
- `scripts/run_ezmark_server.py` — production-style runner (reads `HOST`/`PORT`/`LOG_LEVEL`, works frozen under PyInstaller).
- `packaging/windows/` — PyInstaller specs and build scripts; `tests/test_portable_packages.py`, `tests/test_demo_mode.py` cover the packaged/demo behaviors.
