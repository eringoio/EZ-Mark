"""Seed the synthetic EZ Mark demo environment.

Targets the demo data root only (``DEMO_MODE=1``; ``DEMO_DATA_ROOT``, default
``data-demo/``). Ambient ``DATABASE_URL``/``DATA_ROOT`` values are ignored so
this script can never seed a school's real local database.

Usage:

    python scripts/seed_demo.py          # seed once (no-op when already seeded)
    python scripts/seed_demo.py --reset  # wipe and deterministically reseed
"""

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the EZ Mark demo dataset.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="wipe the demo database/data files and reseed deterministically",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    # Target the separate demo environment; never the normal local dataset.
    os.environ["DEMO_MODE"] = "1"
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("DATA_ROOT", None)

    from app.database import SessionLocal, init_database
    from app.services.demo_service import reset_demo_data, seed_demo_data

    init_database()

    if args.reset:
        reset_demo_data()
        print("Demo data wiped and reseeded (synthetic content only).")
        return 0

    with SessionLocal() as db:
        seeded = seed_demo_data(db)
    if seeded:
        print("Demo dataset seeded (synthetic content only).")
    else:
        print("Demo dataset already seeded; run with --reset to wipe and reseed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
