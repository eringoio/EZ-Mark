# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
repo_root = Path.cwd()
hiddenimports = collect_submodules("app") + collect_submodules("local_app")

# PDF rendering uses redistributable pypdfium2 (BSD/Apache-style), never the
# AGPL-licensed PyMuPDF. pypdfium2 is imported directly by
# app/services/scan_batch_service.py, so PyInstaller's hooks pull in its DLL
# automatically; listed here to keep the dependency explicit in the spec.
hiddenimports.append("pypdfium2")

a = Analysis(
    [str(repo_root / "scripts" / "run_ezmark_server.py")],
    pathex=[str(repo_root)],
    binaries=[],
    datas=[
        (str(repo_root / "app" / "templates"), "app/templates"),
        (str(repo_root / "app" / "static"), "app/static"),
        (str(repo_root / "app" / "locales"), "app/locales"),
        (str(repo_root / ".env.example"), "."),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ezmark-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="server",
)
