# Scanning and Review

How scanned answer sheets get into EZ Mark and how unclear marks are resolved.

## Supported scan formats

- **PDF** — rasterized internally with pypdfium2 at 300 DPI (each page becomes an image).
- **JPG / JPEG / PNG** — one file per page.

Upload via the session's Scans page (bulk selection supported). Uploaded originals and processed pages are stored under the data root; demo mode rejects uploads entirely.

## What happens on upload

1. Each page is rasterized/normalized and stored.
2. The QR code is decoded; the token is matched against the sheet database for that session → the page is mapped to its sheet and student.
3. Bubbles are read by OpenCV against the exact coordinate map stored at PDF-generation time (alignment markers allow small positional drift; the drift search range is configurable in settings).
4. Each bubble becomes a response: chosen answer, fill confidence, and a verdict (confirmed / blank / multiple / invalid / needs review).

Only QR-matched pages proceed automatically. Unmatched or QR-less pages are listed for manual assignment (including spare blank sheets, which can be assigned to a student here).

## Review queue

The review queue lists only what the reader marked as unclear or conflicting:

- **Unclear marks** — fill level between thresholds or two bubbles too close in darkness (both thresholds are tunable in Settings with a live preview).
- **Blank vs multiple-mark** vs **invalid** verdicts.

For each item the reviewer sees the scanned crop image, the detected value, the question, and the correct answer (for Standard Tests), then:

- picks the intended bubble;
- marks the item blank (no answer) or invalid (doesn't count);
- uses hotkeys (answer keys, previous/next) for fast triage;
- applies **bulk page decisions** to mark every remaining unresolved item on the current page blank or invalid.

Peer Evaluation reviews the same way but fixes ratings (1–5, blank, invalid) instead of multiple-choice answers.

## Reprocessing

Uploaded batches can be reprocessed per-session or per-page:

- *Reprocess unmatched pages* re-reads only unmatched pages and keeps manual mappings and review corrections;
- *Reprocess from original upload* resets pages to the original files — this discards manual assignments, blank-sheet assignments, and review corrections for those uploads (with a confirmation dialog).

Deleting an upload removes its processed pages and responses and invalidates calculated results for the session until results are recalculated.

## After review

Calculated results always come from the **final reviewed responses** — the scoring service reads nothing else. When any correction lands, recalculate results (or they're recalculated by the session's finalize step) before trusting exports.
