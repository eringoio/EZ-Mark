# EZ Mark Windows Packaging

This folder contains the first packaging layer for EZ Mark Mode A local desktop use on Windows Server 2016.

The web app remains FastAPI/Jinja2/HTMX. The Windows executable is only a launcher that checks or starts the local server and opens Chrome app mode.

## Prerequisites

- Windows with PowerShell.
- Python environment with the project dependencies installed.
- PyInstaller installed in the build environment.
- Google Chrome installed on the target server.

## Build Server

From the repository root:

```powershell
packaging\windows\build_server.ps1
```

This builds `ezmark-server.exe` in onedir mode under:

```text
dist-windows\EZMark\server\
```

The server package includes application templates, static files, and locales. It must not include local `data/`, `app.sqlite`, uploaded scans, generated PDFs, exports, backups, or real `.env` files.

PyInstaller does not reliably detect Uvicorn string imports such as `uvicorn.run("app.main:app")`. The server runner imports the FastAPI app object directly, and `ezmark-server.spec` also uses `collect_submodules("app")` so the frozen executable includes the full `app` package.

## Build Launcher

From the repository root:

```powershell
packaging\windows\build_launcher.ps1
```

This builds:

```text
dist-windows\EZMark\EZMark.exe
```

It also creates empty `data`, `logs`, and `config` folders and copies `sample.local.env` to `config\local.env`.

At runtime, the launcher derives `DATABASE_URL` and `DATA_ROOT` from the folder containing `EZMark.exe`, unless `EZMARK_ROOT` is explicitly set. The package does not hardcode those paths.

## Deploy To C:\EZMark

After both builds complete, copy the contents of:

```text
dist-windows\EZMark\
```

to:

```text
C:\EZMark\
```

For upgrades, replace only application and server files. Do not delete `C:\EZMark\data`, and do not overwrite an existing `C:\EZMark\data\app.sqlite`.

Back up `C:\EZMark\data` before every upgrade.

The expected deployed layout is:

```text
C:\EZMark\
  EZMark.exe
  server\
    ezmark-server.exe
  config\
    local.env
  data\
  logs\
```

## Test

Run:

```powershell
C:\EZMark\EZMark.exe
```

Expected behavior:

1. `C:\EZMark\logs\launcher.log` is created.
2. The launcher checks `http://127.0.0.1:8000/healthz`.
3. If needed, it starts `C:\EZMark\server\ezmark-server.exe`.
4. It waits up to 90 seconds for the server to become healthy and logs progress.
5. Chrome opens in app mode at `http://127.0.0.1:8000`.

First startup can be slow on older Windows Server machines. The default startup timeout is 90 seconds. Set `EZMARK_STARTUP_TIMEOUT_SECONDS=90` or a higher value if needed; missing, invalid, or lower values fall back to 90 seconds.

For development before packaging:

```powershell
$env:EZMARK_ROOT="$PWD\.runtime"
$env:EZMARK_DEV_SERVER_CMD="python scripts\run_ezmark_server.py"
$env:EZMARK_STARTUP_TIMEOUT_SECONDS="90"
python -m local_app.launcher
```

## Logs

- Launcher log: `C:\EZMark\logs\launcher.log`
- Server log: `C:\EZMark\logs\server.log`

## Data Safety

Do not bundle real data into the package. Builds should create empty runtime folders only.

Updating EZ Mark must preserve:

- `C:\EZMark\data`
- `C:\EZMark\config`
- existing backups under `C:\EZMark\backups`

To reset test data, stop the server and move the test SQLite file out of the way:

```powershell
Rename-Item C:\EZMark\data\app.sqlite C:\EZMark\data\app.sqlite.test-backup
```

Do not run reset commands against production school data.

## Backups

Back up the whole data directory before maintenance:

```powershell
Copy-Item -Recurse C:\EZMark\data C:\EZMark\backups\data-backup-YYYY-MM-DD
```

For Mode A local desktop mode, keep `HOST=127.0.0.1`. Do not use `0.0.0.0`, do not open firewall ports, and do not expose this package to the LAN yet.
