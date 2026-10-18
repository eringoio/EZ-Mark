# Installation

EZ Mark is a local-first Python web application: no database server, no cloud account, no build step required for local use.

## Requirements

- Python 3.10+ (tested on Windows with Python 3.10–3.13; other platforms are untested)
- A modern browser
- Windows is the tested platform; the packaged Windows app additionally needs Google Chrome installed (the launcher opens Chrome in app mode)

## Install from source

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate
pip install -r requirements.txt
```

Supported Python: 3.10–3.13 (the code targets 3.10; the app and test suite are regularly run on 3.13). Developer tools (`pytest`, `ruff`, `httpx`) are in `requirements-dev.txt` and are not required for end users.

## First run

```bash
python scripts/run_ezmark_server.py
```

(or `uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload` for development)

Open `http://127.0.0.1:8000`. The SQLite database and file folders are created automatically under the data root on first start (`data/` by default in a source checkout).

## Configuration

Environment variables (or a local `.env` file copied from `.env.example`):

- `DATA_ROOT` — data root directory (default `data/`)
- `DATABASE_URL` — full database URL override (default SQLite inside the data root)
- `HOST` / `PORT` / `LOG_LEVEL` — read by `scripts/run_ezmark_server.py` (defaults `127.0.0.1:8000`, `info`)
- `DEMO_MODE`, `DEMO_DATA_ROOT`, `DEMO_RESET_ON_START` — see [demo-mode.md](demo-mode.md)

## Packaged Windows app

If you prefer not to run Python, use the packaged Windows build (`EZMark.exe` launcher + `ezmark-server.exe` server). Build instructions and the deployed layout are in [windows-packaging.md](windows-packaging.md) and `packaging/windows/README.md`. Data starts under `%LOCALAPPDATA%\EZMark` (or beside the executable for portable `EZMARK_ROOT` installs).

## Verify

Request `http://127.0.0.1:8000/healthz` — it should return a status JSON. For development testing:

```bash
python -m pytest
```
