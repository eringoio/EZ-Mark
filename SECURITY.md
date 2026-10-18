# Security Policy

## Reporting a vulnerability

Please report security issues privately — e.g. a private security advisory through the repository host's advisory feature, or a direct message to the maintainers if you have their contact details. Do **not** open a public issue with reproduction details for a security problem.

Include: affected area (routes/service/app/launcher), version or commit, environment (dev / packaged Windows / hosted demo), and minimal reproduction steps. Reports are triaged by the maintainers on a best-effort basis.

## Deployment context (worth knowing when reporting)

- EZ Mark is local-first by design. The normal target is a **single-operator install** on a trusted machine (`127.0.0.1`).
- Local installs can contain **personally identifiable data**: student/participant names and numbers (rosters), printed names on generated sheets, and scans of filled answer sheets. They all live under the local data root (`data/` in development, `%LOCALAPPDATA%\EZMark` or the `EZMARK_ROOT` location for packaged installs, `data-demo/` in demo mode) — anyone with file-system access to that directory can read them. Protect it accordingly.
- The QR codes printed on sheets contain **only random opaque tokens** — never names or student numbers.
- There is no built-in authentication. If `HOST=0.0.0.0` is used, anyone who can reach the port can access the app and its data. Prefer localhost binding; if you must bind wider, use network-level access controls.
- Demo mode (`DEMO_MODE=1`) refuses roster/package/scan uploads so a shared demo instance cannot collect personal data, but it otherwise serves seeded synthetic content publicly.

## Out of scope

- SQL injection via SQLite internals, or the single-writer nature of SQLite itself.
- Issues that require an attacker to already have local file-system access on a trusted single-operator machine.
- "Official score" or scoring-accuracy concerns (the app reports raw estimated scores only).
