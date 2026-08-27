# LZT Fortnite Monitor — Railway v1.1

Read-only Fortnite listing monitor for GitHub + Railway. It polls the official LZT Fortnite endpoint, applies a configurable heuristic score, stores seen listing IDs in SQLite, and sends Telegram alerts.

It does not buy listings or automate credential changes. Listings declared as brute/phishing/stealer/support-recovery are excluded from automated monitoring.

## Files

- `monitor.py` — worker loop
- `config.json` — score weights and seed rare cosmetics
- `requirements.txt` — Python dependencies
- `.env.example` — variable names only; never commit real secrets
- `.gitignore` — keeps secrets/local DB out of Git

## Railway

1. Put these files in the repository root.
2. Create a Railway service from the GitHub repository.
3. Add a Volume mounted at `/app/data`.
4. Add service Variables:
   - `LZT_TOKEN`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `POLL_INTERVAL_SECONDS=60`
   - `MIN_PRICE_RUB=500`
   - `MAX_PRICE_RUB=2000`
   - `MIN_DAYBREAK_DAYS=25`
   - `MIN_REVIEW_SCORE=45`
   - `MIN_STRONG_SCORE=70`
   - `DATA_DIR=/app/data`
5. Start command: `python monitor.py`
6. Check deployment logs for `Starting monitor` and `Fetched ... items`.

No public domain is required because this is a background worker, not an HTTP server.

## Local test

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill real secrets locally only
python monitor.py
```
