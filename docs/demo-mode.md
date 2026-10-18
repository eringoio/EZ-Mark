# Demo Mode

EZ Mark has one optional, clearly separated demo mode for shared public demos.
It is off by default and never affects normal local use.

## Enabling

```text
DEMO_MODE=1
DEMO_RESET_ON_START=1   # optional: wipe and reseed on every server start
DEMO_DATA_ROOT=data-demo  # optional override; default data-demo/
```

`DEMO_RESET_ON_START` requires `DEMO_MODE`. With it, every server start wipes
the demo rows and file directories and reseeds the synthetic dataset — good for
a shared demo, never enabled automatically otherwise.

## Separate data root

A demo run never opens the normal `data/` directory. With `DEMO_MODE=1` and no
explicit `DATABASE_URL`/`DATA_ROOT`, the app automatically uses:

- SQLite database: `data-demo/app.sqlite`
- generated PDFs / uploaded scans / processed pages / review crops / exports /
  previews: subdirectories of `data-demo/`

Explicit `DATABASE_URL` or `DATA_ROOT` still overrides this (the same setting
rule as normal mode).

## Demo indicator

Every page shows a "Demo Mode" / デモモード label in the header plus a banner
stating that the content is fully synthetic and may be reset at any time.

## Restricted endpoints

So a shared public demo cannot become a collector of personal data, demo mode
rejects (HTTP 403, localized message) the following persistent upload/import
endpoints:

- student roster CSV/XLSX import (preview and confirm);
- portable template/session package import (preview and confirm, both kinds);
- scan uploads for Standard Test and Peer Evaluation runs.

There is no scan upload in the demo dataset; seeded content provides the
"example" data instead. Normal local mode keeps all of these routes fully
functional — the guard only fires when `DEMO_MODE` is on.

## Seeding

```bash
python scripts/seed_demo.py          # seed once (no-op when already seeded)
python scripts/seed_demo.py --reset  # wipe and deterministically reseed
```

The seed script forces demo settings and ignores ambient `DATABASE_URL` /
`DATA_ROOT`, so it can never write to an existing real database.

Synthetic contents:

- **Demo Quiz: Multiple Choice (20 Questions)** — template plus a run
  ("Demo Quiz Run (Demo Group A)"): 20 four-choice questions worth 1 point each,
  continuous numbering, fictional answer key (answers cycle 1–4);
- **Demo Peer Evaluation Example** — peer-review template plus a run, using
  verified peer-review services;
- 12 fictional participants ("Demo Group A/B", numbers `D2101`–`D2112`) attached
  to both runs.

Seeding is deterministic and idempotent: running it twice records nothing new,
and `--reset` always rebuilds the exact same dataset. No scan images are
shipped or generated, and no real personal or institutional data ever enters
the demo dataset.
