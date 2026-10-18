# Architecture

This document provides a high-level summary of the architecture of **EZ Mark**.

## Overview

The application is built as a single, lightweight, server-rendered Python web application. The presentation layer relies on FastAPI and Jinja2 with HTMX for reactive components, while the core business logic, OMR scan processing, and PDF generation live in backend service modules.

```
       +---------------------------------------------+
       |                  Browser                    |
       |    (Jinja2 Server-Rendered HTML + HTMX)     |
       +----------------------|----------------------+
                              | HTTP / Forms / Files
       +----------------------v----------------------+
       |                 FastAPI                     |
       |           (app/routes/ Layer)               |
       +----------------------|----------------------+
                              |
       +----------------------v----------------------+
       |             Application Services            |
       |           (app/services/ Layer)             |
       +---|-------------|-------------|-------------|
           |             |             |
+----------v---------+   |   +---------v---------+
|     SQLite DB      |   |   |   ReportLab PDF   |
|   (SQLAlchemy)     |   |   |     Generator     |
+--------------------+   |   +-------------------+
                         |
             +-----------v-----------+
             |      OMR Engine       |
             |   (OpenCV + pypdfium2) |
             +-----------------------+
```

## Stack

- **Frontend**: HTML5, vanilla CSS, Jinja2 templates, HTMX (for asynchronous dynamic updates), vanilla JS for the stateful template/peer builders
- **Backend framework**: FastAPI (ASGI server run via Uvicorn)
- **Database**: SQLite (local single-file database) accessed through SQLAlchemy
- **PDF generation**: ReportLab (exact A4 layout control)
- **Image & OMR processing**: OpenCV, NumPy; pypdfium2 (PDF page-to-image conversion, BSD/Apache-licensed); qrcode + Pillow (QR rendering and image canvas helpers)
- **Data imports/exports**: openpyxl (Excel parsing and creation), standard-library `csv`
- **Localization**: JSON dictionaries (`app/locales/en.json`, `app/locales/ja.json`) with a small i18n middleware/service

## Main modules

| Module | Responsibility |
|---|---|
| **`app.routes`** | Routing, form submissions, upload handlers, and HTMX partial endpoints. |
| **`app.models`** | SQLAlchemy declarative models (`mock_exams` is the internal table name for templates/test runs — a schema-v1-locked historical name). |
| **`app.services.test_template_service`** | Template CRUD, run creation from templates, template versioning/archiving. |
| **`app.services.exam_service`** | Test-run lifecycle: creation, state transitions, guards (template/archived), deletion. |
| **`app.services.student_import_service`** | Parses CSV/XLSX spreadsheets, validates headers and rows, resolves conflicts. |
| **`app.services.answer_key_service`** | Question-structure generation and answer-key validation/storage. |
| **`app.services.sheet_generation_service`** | Builds coordinate maps, generates PDF mark sheets with QR tokens and alignment markers. |
| **`app.services.qr_token_service`** | Generates and resolves unique random opaque tokens for sheet-to-student mapping. |
| **`app.services.scan_batch_service`** | Accepts PDF/JPG/JPEG/PNG uploads, normalizes pages, drives page processing. |
| **`app.services.bubble_reader_service`** | Aligns scans and reads bubble fill density via OpenCV against stored coordinates. |
| **`app.services.review_service`** | Unclear-mark queue: crop images, review saves, manual fixes, bulk page actions. |
| **`app.services.scoring_service`** | Raw-point totals, percentages, pass/fail, question analysis from final reviewed responses. |
| **`app.services.peer_review_*`** | Peer Evaluation forms, layout/pagination, rating workflows, insights. |
| **`app.services.export_service`** | XLSX/CSV exports from final responses and result records. |
| **`app.services.portable_package_service`** | Template/session export/import packages (public package schema v2, `manifest.json` `schema_version`; incompatible versions are rejected with a generic error, never reinterpreted) with QR-collision handling. |
| **`app.services.demo_service`** | Synthetic demo dataset, demo-mode guards, demo root isolation. |
| **`app.database`** | Engine/session setup, data-root directories, schema v1 marker and `IncompatibleSchemaError` policy. |

## Data model

The schema is a single versioned public baseline (schema marker `schema_info.schema_version = 1`). Key tables include:

- `students` / `mock_exams` / `exam_students` — core roster and template/run records
- `physical_sheets` / `sheet_templates` / `sheet_instances` — OMR sheet tracking and bubble coordinate maps
- `questions` / `scoring_sections` / `problem_groups` — question structure and answer keys
- `peer_review_*` tables — competencies, forms, pages, targets, ratings
- `scan_batches` / `scanned_pages` / `responses` — uploaded scans and read bubble values
- `student_results` / `exports` — aggregated scores/pass-fail and export metadata

All tables are declared in `app/models/` and mirror the schema v1 baseline; startup refuses databases with a different marker (`IncompatibleSchemaError`) instead of migrating or modifying them.

## State ownership

Where important state lives:

| State | Source of truth | Notes |
|---|---|---|
| **Student rosters** | Database (`students`, `exam_students`) | Imported via CSV/XLSX or manual entry. |
| **Templates, answer keys & points** | Database (`mock_exams`, `questions`, `scoring_sections`, `problem_groups`) | Configured in the template builder. |
| **Bubble coordinates** | Database (`sheet_templates`) | Captured and stored at the *time of PDF generation*; the reader uses these exact coordinates. |
| **Read marks/responses** | Database (`responses`) | Populated by the OpenCV OMR reader; corrected only via the review queue. |
| **Scores & pass/fail** | Database (`student_results`) | Recalculated by `scoring_service` whenever final responses change. |
| **Scans / PDFs / crops / exports** | File system (data root) | Local files; database references are relative paths. |
| **Locale** | Cookie (`lang`) | en/ja dictionaries in `app/locales/`. |

## Storage layout

Everything persistent lives under the configured data root with fixed subdirectories (`uploaded_scans/`, `processed_pages/`, `generated_pdfs/`, `review_crops/`, `exports/`, `import_previews/`, `package_exports/`). See [data-storage-and-privacy.md](data-storage-and-privacy.md) for locations per mode.

## Packaging variants

- **Development/dev server**: `uvicorn` or `scripts/run_ezmark_server.py` with a repo-side `data/` root.
- **Packaged Windows**: PyInstaller-built `ezmark-server.exe` + `EZMark` launcher (see `packaging/windows/`), per-user or portable data roots.
- **Hosted/demo**: the same app served bound wider, ideally with `DEMO_MODE=1` and a single worker (SQLite is single-writer). See [hosted-deployment.md](hosted-deployment.md).

## Important invariants

- **PII isolation**: QR codes on mark sheets contain only random, database-trackable opaque tokens — never student names, numbers, or other PII.
- **Coordinate correspondence**: the scan processor loads the coordinate template saved for that exact sheet version; coordinates are never re-derived or hardcoded.
- **Scoring**: question `points` is the single canonical weight; totals are exact (fractional points never rounded up). Pass/fail requires an explicit pass mark (`pass_mark_value` + `pass_mark_type` of `points` or `percentage`); with no pass mark, results get a neutral `scored` status. Only final reviewed responses drive scoring.
- **ISO-8601 dates**: date-only values are stored as plain `YYYY-MM-DD` strings.
- **Fail loudly**: imports validate headers and rows and reject problems instead of coercing or skipping them.

## License posture

The project is MIT licensed; third-party library notices are collected in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) (PDF rasterization uses redistributable pypdfium2 rather than AGPL-licensed alternatives).
