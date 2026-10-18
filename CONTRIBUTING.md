# Contributing

Thanks for considering a contribution to EZ Mark!

## Development setup

```bash
python -m venv venv
venv\Scripts\activate           # Windows
source venv/bin/activate        # macOS/Linux
pip install -r requirements.txt -r requirements-dev.txt
```

## Run locally

```bash
venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000`. Data lives in the local `data/` directory.

### Demo dataset (optional)

```bash
set DEMO_MODE=1                 # Windows cmd; export DEMO_MODE=1 on sh
venv\Scripts\python.exe scripts\seed_demo.py --reset
venv\Scripts\python.exe scripts\run_ezmark_server.py
```

## Tests and lint

```bash
venv\Scripts\python.exe -m pytest
venv\Scripts\python.exe -m ruff check .
```

Please make sure both pass before submitting a pull request. New behavior should come with tests; visible UI strings must exist in both `app/locales/en.json` and `app/locales/ja.json`.

## Ground rules

- **Synthetic data only.** Never commit, seed, share, or screenshot real student/participant rosters, names, numbers, scans, or answer keys. Tests and docs use fully fictional data (e.g. the seeded demo dataset in `scripts/seed_demo.py`).
- **Keep the invariants:** QR codes carry random opaque tokens only (no PII); bubble coordinates are saved at PDF-generation time and reused when reading scans; file paths stored in the database are relative to the data root; imports fail loudly on bad headers/duplicates; scoring uses final reviewed responses; new schema stays behind the public schema v1 marker.
- **Keep terminology neutral and consistent:** Use "Group" rather than "Class" in visible wording, keep both English and Japanese locales in sync, and keep product copy free of any specific institution or domain context.
- Make the smallest safe change; don't refactor unrelated code in the same PR.

## License

By contributing you agree that your contributions are licensed under the MIT License (see `LICENSE`).
