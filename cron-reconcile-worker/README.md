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
# Damaged Book Reconcile Cron Worker

This Railway-native worker executes the same reconciliation logic as the `/admin/reconcile` endpoint from the main Damaged Books Service (DBS). It runs on a scheduled basis using Railway Cron Jobs and performs GQL inventory checks, applies publish/unpublish logic, and exits cleanly.

---

## 🧠 Overview

This worker:

- Imports the actual `reconcile_damaged_inventory()` function from the DBS codebase
- Executes the reconciliation logic on boot
- Logs the outcome (inspected, updated, skipped, or any errors)
- Exits immediately after running — compliant with Railway Cron Job expectations

---

## 📂 File Structure

```
cron-reconcile-worker/
├── index.py                  # Entrypoint – calls DBS logic and logs results
├── requirements.txt          # Runtime dependencies (e.g. python-dotenv)
├── .env.example              # Example env vars (copy into Railway or local .env)
```

---

## 🛠️ Setup Locally

```bash
cd cron-reconcile-worker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# then fill in your environment values
python index.py
```

---

## 🧪 Example Output (Success)

```
✅ Reconcile completed
{'inspected': 43, 'updated': 28, 'skipped': 15}
```

If `SHOPIFY_LOCATION_ID` is missing, it will return:

```
✅ Reconcile completed
{'inspected': 0, 'updated': 0, 'skipped': 0, 'note': 'missing SHOPIFY_LOCATION_ID'}
```

---

## 📦 Required Environment Variables

These must be configured in both local `.env` and Railway service settings:

| Variable                   | Description                                      |
|----------------------------|--------------------------------------------------|
| `SHOP_URL`                | Your Shopify store domain (e.g. `your-store.myshopify.com`) |
| `SHOPIFY_API_KEY`         | Admin API public key                             |
| `SHOPIFY_API_SECRET`      | Admin API secret                                 |
| `SHOPIFY_ACCESS_TOKEN`    | Admin API access token (scoped)                  |
| `SHOPIFY_LOCATION_ID`     | Shopify location ID (used in GraphQL)            |
| `SUPABASE_URL`            | Supabase project URL                             |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase Service Role key                     |

---

## 🚀 Deploy on Railway

1. Push this folder to your GitHub repo inside the `damaged-books-service` monorepo.
2. In Railway:
   - Click **“New Service” → “Deploy from GitHub”**
   - Enable **Monorepo** support
   - Set **Root Directory** to: `cron-reconcile-worker`
3. Set the **Start Command** to:
   ```bash
   python index.py
   ```
4. In **Settings → Variables**, set all required `.env` values
5. In **Settings → Cron Jobs**, add:
   ```
   Cron Expression: 0 2 * * *       # Every day at 2:00 AM UTC
   ```

---

## 🖥️ Surface to Admin Dashboard (DBS Consumer UI)

The latest reconcile result is intended to be displayed inside the Admin Dashboard:

- 📊 **Table Component** → Will show results such as:
  - `inspected`, `updated`, `skipped`, `note`, `at`
- 🔘 **"Reconcile now" button** → Should manually trigger POST `/admin/reconcile` with header:
  ```http
  X-Admin-Token: ${VITE_DBS_ADMIN_TOKEN}
  ```

---

## 🧩 Status Endpoint

The DBS also exposes:

```
GET /admin/reconcile/status
```

Returns the last known reconcile run (once logging is persisted). Currently returns:

```json
{ "last_run": null }
```

---

## ✅ Example: Manual Trigger via curl

```bash
BASE="https://used-books-service-production.up.railway.app"
curl -i -X POST "$BASE/admin/reconcile" \
  -H "X-Admin-Token: $VITE_DBS_ADMIN_TOKEN"
```

Expected JSON:
```json
{
  "inspected": 42,
  "updated": 39,
  "skipped": 3,
  "at": "2025-08-18T12:34:56.789Z"
}
```

---

## 🧼 Exit Codes

- `0`: success
- `1`: failure

You can view results directly in Railway Logs.