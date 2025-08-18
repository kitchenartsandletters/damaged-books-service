# Damaged Book Reconcile Cron Worker

This Railway-native worker executes the `/admin/reconcile` logic from the main Damaged Books Service.

## How It Works

- This worker imports and runs `run_reconcile()` from the DBS repo
- On startup, it performs the reconcile logic (GQL inventory check → publish/unpublish)
- Logs result and exits (Railway compliant)

## Setup

```bash
cd cron-reconcile-worker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in your secrets
python index.py