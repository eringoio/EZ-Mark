# Third-Party Notices

EZ Mark is released under the MIT License (see `LICENSE`). This file records the
third-party packages and bundled binaries that EZ Mark depends on or ships with,
with the license each is distributed under and any attribution required.

Version numbers below reflect the versions the project was developed and tested
against (October 2026). All licenses listed are permissive and compatible with
MIT redistribution; no runtime dependency is AGPL/GPL.

## Direct runtime dependencies (Python)

| Package | Version (tested) | License | Notes / attribution |
| --- | --- | --- | --- |
| FastAPI | 0.143.3 | MIT | Copyright (c) 2018 Sebastián Ramírez |
| Starlette | 1.8.0 | BSD-3-Clause | Copyright (c) 2018, Encode OSS Ltd |
| Uvicorn | 0.54.0 | BSD-3-Clause | Copyright (c) 2017-present, Encode OSS Ltd. The `uvicorn[standard]` extra pulls MIT/BSD-licensed helpers (`httptools` — MIT plus BSD-licensed vendored `llhttp`/`http-parser` from Node.js; `websockets` — BSD-3-Clause; `watchfiles` — MIT; `colorama` — MIT). None are GPL. |
| Jinja2 | 3.1.6 | BSD-3-Clause | Copyright 2007 Pallets |
| python-multipart | 0.0.32 | Apache-2.0 | Copyright Andrew Godwin & contributors |
| SQLAlchemy | 2.1.5 | MIT | Copyright 2005-2026 SQLAlchemy authors and contributors |
| Alembic | 1.20.0 | MIT | Copyright 2009-2026 Michael Bayer |
| Pydantic | 2.14.1 | MIT | Copyright (c) 2017 to present Pydantic Services Inc. and individual contributors |
| pydantic-settings | 2.16.0 | MIT | Copyright (c) 2022 Samuel Colvin and other contributors |
| opencv-python | 5.0.0.93 | Apache-2.0 | The wheel bundles the OpenCV library; its license and third-party notices are reproduced in the wheel's `LICENSE.txt` and `LICENSE-3RD-PARTY.txt`. |
| NumPy | 2.5.4 | BSD-3-Clause | Copyright (c) 2005-2025, NumPy Developers. The wheel carries additional permissive notices for vendored sub-projects (LAPACK, pocketfft, PCG64, x86-simd-sort, highway, SVML, libdivide). |
| pypdfium2 | 5.14.0 | BSD-3-Clause / Apache-2.0 | Python bindings to PDFium. Copyright 2022 pypdfium2-team. See "Bundled binary" section below. |
| ReportLab | 5.0.1 | BSD-3-Clause | Copyright (c) 2000-2025, ReportLab Inc. |
| qrcode | 8.2 | BSD-3-Clause | Copyright (c) 2011, Lincoln Loop |
| Pillow | 13.0.0 | MIT-CMU (HPND) | Copyright © 1997-2011 by Secret Labs AB; Copyright © 1996-2026 Fredrik Lundh and contributors. Permissive; requires the copyright notice reproduction above. |
| openpyxl | 3.1.5 | MIT | Copyright (c) 2010 openpyxl |
| python-dotenv | 1.2.4 | BSD-3-Clause | Copyright (c) 2013, Saurabh Kumar |
| httpx | 0.28.1 | BSD-3-Clause | Copyright © 2019-2023 Encode OSS Ltd / Tom Christie |

## Dev-only tooling (not runtime requirements)

These are developer tools only and are not needed to run EZ Mark; they are kept
in `requirements.txt` for local development until they are split out.

| Package | Version (tested) | License |
| --- | --- | --- |
| pytest | 9.1.1 | MIT (Copyright (c) 2004 Holger Krekel and others) |
| ruff | 0.17.1 | MIT (Copyright (c) 2022 Charles Marsh) |

## Bundled binary: PDFium (via pypdfium2)

The `pypdfium2` wheel bundles a compiled **PDFium** library (Google's PDF engine,
license: **BSD-3-Clause**, Copyright © 2025 Google LLC — see `pdfium.txt`) plus
audited third-party components with permissive licenses reproduced in the
pypdfium2 wheel under `pypdfium2-5.14.0.dist-info/licenses/data/<platform>/BUILD_LICENSES/`:

- abseil-cpp (Apache-2.0)
- agg23 (compatible permissive license, epagmerkle/AGG originals)
- fast_float (Apache-2.0 / MIT)
- FreeType (FTL — BSD-style with additional notices)
- ICU (ICU License, formerly MIT/X; permissive)
- lcms2 (MIT)
- libjpeg-turbo (IJG / BSD-3-Clause)
- libopenjpeg (BSD-2-Clause)
- libpng (PNG license — permissive)
- john-kim llvm-libc portions / simdutf (MIT / Apache-2.0 style)
- zlib (permissive)

None of these impose copyleft obligations; redistribution of the wheel
(`pip install pypdfium2`) keeps all notices intact automatically. If you
distribute a packaged build (e.g. PyInstaller bundle) you must retain the
pypdfium2 package license files alongside the bundled PDFium binary.

## Bundled frontend assets

No third-party JavaScript or chart libraries are bundled; the frontend is
server-rendered Jinja2 templates with first-party vanilla CSS/JS in
`app/static/`. Any future bundling must be re-audited.

## Fonts

EZ Mark does **not** bundle any font files. PDF sheets are produced with
ReportLab built-in fonts and, for Japanese text, with one of:

1. a system-installed font found at runtime (Noto Sans CJK, Yu Gothic, Meiryo,
   or MS Gothic — see `_register_japanese_font()` in
   `app/services/sheet_generation_service.py`); or
2. ReportLab's built-in Adobe CID fonts (`HeiseiKakuGo-W5`, `HeiseiMin-W3`),
   which are *referenced, not embedded*.

Fonts used from the operating system are governed by the OS vendor's license
and are not redistributed by EZ Mark. No font file from a developer machine is
copied into the repository.
