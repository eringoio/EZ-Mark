# EZ Mark Local App Launcher

`local_app` is the optional Windows launcher layer for EZ Mark Mode A local desktop use.

The web app remains the real application. This package only starts or finds the local FastAPI server and opens Google Chrome in app mode at `http://127.0.0.1:8000`.

Architecture rules:

- `app/` must not import `local_app/`.
- `local_app/` should stay independent and standard-library-only where practical.
- Chrome app mode is preferred; the default browser is only a fallback when Chrome is not found.
- The target environment is Windows Server 2016, local or RDP use.
- This mode binds to `127.0.0.1` and does not expose EZ Mark to the LAN.

Runtime path rules:

- If `EZMARK_ROOT` is set, that folder is the app root.
- If packaged with PyInstaller, the folder containing `EZMark.exe` is the app root.
- In development, the repository root is the app root.
- If `EZMARK_DEV_SERVER_CMD` is set, the launcher treats this as explicit development server mode and starts that command from the repository root, even when `EZMARK_ROOT` points at a separate runtime folder.
- Data: `<app_root>\data`
- Logs: `<app_root>\logs`
- Config: `<app_root>\config`
- Server executable: `<app_root>\server\ezmark-server.exe`
- Database: `<app_root>\data\app.sqlite`
- Chrome profile: `%LOCALAPPDATA%\EZMark\chrome-profile` on Windows, with a home-directory fallback if `LOCALAPPDATA` is unavailable.
- Chrome launch stderr log: `%LOCALAPPDATA%\EZMark\logs\chrome-launch.log` on Windows, with the same home-directory fallback.

The Chrome profile is intentionally per Windows user and is not stored under the shared app root. This avoids permission and profile-lock conflicts when multiple users launch EZ Mark from the same installation folder.

Development usage:

PowerShell:

```powershell
$env:EZMARK_ROOT="$PWD\.runtime"
$env:EZMARK_DEV_SERVER_CMD="python scripts\run_ezmark_server.py"
$env:EZMARK_STARTUP_TIMEOUT_SECONDS="90"
python -m local_app.launcher
```

Windows CMD:

```bat
set EZMARK_ROOT=%CD%\.runtime
set EZMARK_DEV_SERVER_CMD=python scripts\run_ezmark_server.py
set EZMARK_STARTUP_TIMEOUT_SECONDS=90
python -m local_app.launcher
```

Development fallback is explicit. If `<app_root>\server\ezmark-server.exe` does not exist, the launcher only uses `EZMARK_DEV_SERVER_CMD` when that environment variable is set.

When testing with `.runtime`, the launcher creates `.runtime\data`, `.runtime\logs`, and `.runtime\config`, but resolves relative paths in `EZMARK_DEV_SERVER_CMD` from the repository root. For example, `python scripts\run_ezmark_server.py` runs `scripts\run_ezmark_server.py` from this repository, not from `.runtime`.

First startup can be slow while Python imports and initializes the local app. The launcher waits up to 90 seconds by default and logs health-check progress in `logs\launcher.log`. You can set `EZMARK_STARTUP_TIMEOUT_SECONDS=90` or a higher value for slower machines; missing, invalid, or lower values fall back to 90 seconds.
