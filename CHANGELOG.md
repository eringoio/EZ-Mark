# Changelog

## [1.0.0] — Initial public release

First public version of EZ Mark, the local-first OMR scanner app for Standard Tests and Peer Evaluation.

### Added

- Standard Tests: multiple-choice OMR answer sheets scored with raw points and percentages, optional pass marks (points or percentage), group-based question structures with continuous or grouped numbering, scoring sections, and question-level analysis with review priorities.
- Peer Evaluation: anonymous rating forms where participants rate one another 1–5 per competency; templates with rating scale, rater groups, page layout controls (orientation, density, furigana/ruby support), per-session rosters, page pagination, scan batches, review queue, and results with overall ranking, group comparison, and competency averages.
- Reusable test templates with a visual builder; separate test runs created from templates.
- Student roster import from Excel/CSV with loud validation, import preview, and duplicate/conflict resolution; manual roster entry; roster export.
- Pre-filled PDF mark sheet generation with QR-opaque-token matching, alignment markers, spare blank sheets, per-run sheet regeneration, and printable answer-key sheets.
- Scan upload (PDF/JPG/JPEG/PNG) with automatic QR matching, unmatched/blank page assignment, per-upload and per-page reprocessing controls.
- Unclear-mark review workflow with scanned crop previews, hotkeys, bulk page decisions, and manual sheet assignment.
- XLSX/CSV exports: student summary, per-question answers, and question analysis.
- Dashboard, scan-and-review, and insights views with filters, search, sorting, and pagination; archive (with scan file cleanup), deletion, and bulk actions.
- Adjustable OMR settings (mark detection thresholds, print lightness) with live preview.
- Japanese/English UI (en/ja locales).
- Demo mode: separate `data-demo/` storage, synthetic seed dataset via `scripts/seed_demo.py` (with `--reset`), demo banner/indicator, and disabled uploads/imports in shared demo environments.
- Local packaging for Windows: `ezmark-server.exe` (PyInstaller onedir) and the `EZMark` desktop launcher under `packaging/windows/`, with per-user `%LOCALAPPDATA%\EZMark` and portable `EZMARK_ROOT` data locations.
- Positioned storage: data-root-relative file paths (SQLite records), per-user/portable/demo data roots, schema v1 marker with hard refusal of incompatible databases.

### Removed

- Nothing public; this is the first public baseline.
