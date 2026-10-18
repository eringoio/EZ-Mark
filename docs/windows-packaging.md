# Windows Packaging

PyInstaller spec files live in `packaging/windows/`:

- `build_server.ps1` → `ezmark-server.exe` (onedir, from `packaging/windows/ezmark-server.spec`);
- `build_launcher.ps1` → the `EZMark` desktop launcher.

## What the server spec includes

`packaging/windows/ezmark-server.spec` bundles:

- `scripts/run_ezmark_server.py` as the entry point, with all `app` and `local_app` submodules as hidden imports;
- `app/templates`, `app/static`, `app/locales`, and `.env.example` as data files;
- compiled dependencies (`pypdfium2` for PDF rendering, `opencv-python`, `reportlab`, etc.) via automatic PyInstaller binary collection.

The app does not use AGPL PyMuPDF for PDF rasterization — PDF processing uses the redistributable BSD/Apache-licensed `pypdfium2`. No Alembic migration assets are shipped or needed: startup uses `create_all` + the `schema_info` version marker (`app/database.py`), so there is no migration directory to bundle.

## Executable names

`ezmark-server` (server) and `EZMark` (launcher) are preserved; `README.md` in the same folder documents the build commands.

## Data paths at runtime

The packaged server does not assume its install directory is writable. On start, `scripts/run_ezmark_server.py` detects a frozen (`PyInstaller`) run and defaults persistent data to the per-user location (`%LOCALAPPDATA%\EZMark\data` on Windows), unless `DATA_ROOT`/`DATABASE_URL` are set — the override used for portable installs (for example `EZMARK_ROOT`, which makes data/logs/config live beside the executable). Logs, configuration and backups handled by the `EZMark` launcher live under `%LOCALAPPDATA%\EZMark` (`logs/`, `config/`, `backups/`); see `docs/data-storage-and-privacy.md`.

## Deploying (portable layout example)

After both builds, the `dist-windows\EZMark\` contents (launcher, `server\`, empty `data\`, `logs\`, `config\`) can be copied to a target folder such as `C:\EZMark\`. Keep `data\`, `config\`, and `backups\` when upgrading; replace only application/server files. Back up the data directory before every upgrade.

## Launcher behavior

1. Launcher log: `logs\launcher.log`.
2. It checks `http://127.0.0.1:8000/healthz`.
3. If needed it starts `server\ezmark-server.exe` and waits (default startup timeout 90 seconds; `EZMARK_STARTUP_TIMEOUT_SECONDS` overrides) — first startup on older machines can be slow.
4. It opens Google Chrome in app mode at `http://127.0.0.1:8000` (Chrome must be installed).

Server log: `logs\server.log`.

## Data safety

Do not bundle real data into packages — builds create empty runtime folders only. Never delete or overwrite the deployed `data\app.sqlite` during an upgrade. For test resets, stop the server and rename the test database file aside.

Keep the packaged launcher bound to `HOST=127.0.0.1`; the desktop package is a single-operator local tool, not a network service.
