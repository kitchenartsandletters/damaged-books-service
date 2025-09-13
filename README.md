
# Damaged Books Service (DBS)

The Damaged Books Service (DBS) is a FastAPI backend that processes Shopify inventory level webhooks for used/damaged book variants, applies business rules (publish/unpublish, SEO canonical, redirects), and persists a normalized view of damaged inventory in Supabase. It is designed to be driven by the Webhook Gateway, which relays Shopify webhooks with original headers and body for robust HMAC verification and replay support.

---

## System Overview

### Modern Flow
1. **Webhook ingestion:** Shopify emits `inventory_levels/update` → Gateway receives, verifies Shopify HMAC, and forwards the raw POST (body + headers) to DBS `/webhooks/inventory-levels`.
2. **HMAC verification:** DBS verifies the Shopify HMAC signature (defense-in-depth).
3. **Variant & product resolution:** 
    - First, REST `/admin/variants.json?inventory_item_ids=...` (with post-filtering for exact `inventory_item_id`).
    - Always falls back to GraphQL if REST is ambiguous or empty, to reliably map `inventory_item_id` to `variant_id`, `product_id`, and handle.
4. **Condition extraction:** 
    - DBS queries Shopify GraphQL for the variant’s `selectedOptions` (esp. `Condition`) to determine if this is a damaged book and which condition (Light, Moderate, Heavy).
    - No longer uses handle regex for detection.
5. **Persistence:** 
    - DBS calls Supabase function `damaged.damaged_upsert_inventory(...)` to upsert the normalized row in `damaged.inventory`.
    - View `damaged.inventory_view` is used for queries; `damaged.changelog` is available for auditing.
6. **Business rules:** 
    - If the variant is a damaged book, DBS checks stock (via inventory_service), publishes/unpublishes (product_service), sets canonical (seo_service), and manages redirects (redirect_service).
    - All rules are idempotent and run only for recognized damaged variants.
7. **Response:** 
    - Always returns 200 for app errors (to avoid Shopify retries), 401 only for HMAC signature failures.

---

## Key Services and Responsibilities

- **inventory_service:** Checks real-time stock via Shopify GraphQL (`inventoryLevel(inventoryItemId, locationId)`), used for all availability logic.
- **used_book_manager:** Orchestrates the main flow: receives inventory updates, resolves variant/product, extracts condition, persists to Supabase, and triggers business rules.
- **product_service:** Publishes or unpublishes the damaged book product on Shopify as needed.
- **seo_service:** Updates SEO canonical links so damaged book pages canonicalize to the new/primary product.
- **redirect_service:** Creates or removes redirects from damaged book URLs to the canonical product.

---

## Supabase Schema

- **Schema:** `damaged`
- **Table:** `damaged.inventory` (PK: `inventory_item_id`)
- **View:** `damaged.inventory_view` (adds computed stock status and joins)
- **Changelog:** `damaged.changelog` (optional/future)
- **Upsert Function:** `damaged.damaged_upsert_inventory(...)` (SECURITY DEFINER, sets `search_path` to `public, damaged`)

---

## Damaged-Book Detection (Current)

- **Detection is based on Shopify variant options:**
    - The variant must have a `Condition` option with value `Light`, `Moderate`, or `Heavy`.
    - This is extracted via Shopify GraphQL (`selectedOptions`), not by handle regex.
- **Handles and tags are not used for detection.**
- **If the variant is not a damaged book, the event is skipped.**

---

## Endpoints

- `GET /health` — Health check. Returns `{"status":"ok"}`
- `POST /webhooks/inventory-levels` — Main webhook endpoint. Expects Shopify HMAC, original body, and headers (via Gateway).
- **Admin endpoints (require `X-Admin-Token`):**
    - `GET /admin/docs` — Link hub for admin tools.
    - `GET /admin/damaged-inventory` — List current damaged inventory (header: `X-Result-Count`).
    - `POST /admin/reconcile` — Run a full reconcile/refresh pipeline (see below).
    - `GET /admin/reconcile/status` — Latest reconcile stats.
- **Utility endpoints:** (for diagnostics/dev)
    - `POST /api/products/check` — Manually trigger inventory logic for a specific variant.
    - `POST /api/products/scan-all` — Batch scan (placeholder).
    - `GET /api/products`, `GET /api/products/{product_id}`, `PUT /api/products/{product_id}/publish|unpublish` — Product helpers.
    - `GET/POST/DELETE /api/redirects[...]` — Redirect helpers.

---

## Webhook Gateway Integration

- The Gateway **must** forward:
    - All original Shopify headers, especially `X-Shopify-Hmac-Sha256`.
    - The exact raw request body (no re-stringification).
    - Optionally, `X-Gateway-Event-ID` (for idempotency), `X-Available-Hint` (mirrors `available`), and Gateway-side HMAC signature headers.
- DBS verifies Shopify HMAC (using `SHOPIFY_API_SECRET`).
- If present, DBS logs Gateway HMAC and event ID for traceability.

---

## Replay & Reconcile Pipeline

- **Replay:** Gateway can replay any webhook delivery by ID, forwarding the exact raw bytes and headers. DBS verifies HMAC and processes as if live.
- **Reconcile:** Admin endpoint `/admin/reconcile` walks all rows in `damaged.inventory_view`, fetches current Shopify inventory via GraphQL, and upserts latest status via `damaged_upsert_inventory`. This ensures DBS/Supabase state matches Shopify reality (even if webhooks were dropped).
- **End-to-end tests:** Confirm that live webhooks, replayed events, and reconcile runs all result in correct Supabase state and business rule execution.

---

## Health Checks & curl Examples

```sh
export VITE_DBS_BASE_URL="https://used-books-service-production.up.railway.app"
export VITE_DBS_ADMIN_TOKEN="YOUR_LONG_RANDOM_TOKEN"

# Health
curl -i "$VITE_DBS_BASE_URL/health"
# expect: 200 {"status":"ok"}

# Admin docs
curl -i "$VITE_DBS_BASE_URL/admin/docs" -H "X-Admin-Token: $VITE_DBS_ADMIN_TOKEN"
# expect: 200 + JSON

# List damaged inventory
curl -i "$VITE_DBS_BASE_URL/admin/damaged-inventory" -H "X-Admin-Token: $VITE_DBS_ADMIN_TOKEN"
# expect: 200, header X-Result-Count: <n>

# Trigger reconcile
curl -i -X POST "$VITE_DBS_BASE_URL/admin/reconcile" -H "X-Admin-Token: $VITE_DBS_ADMIN_TOKEN"
# expect: 200 {"inspected":N,"updated":M,"skipped":K}

# Reconcile status
curl -i "$VITE_DBS_BASE_URL/admin/reconcile/status" -H "X-Admin-Token: $VITE_DBS_ADMIN_TOKEN"
# expect: 200 {"inspected":N,"updated":M,"skipped":K,"note":..., "at":"..."}
```

---

## Local Development & HMAC Test

```sh
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Set env (or use .env)
export SHOP_URL=your-store.myshopify.com
export SHOPIFY_API_SECRET=...
export SHOPIFY_ACCESS_TOKEN=...

python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
# http://127.0.0.1:8000/health  -> {"status":"ok"}

# HMAC test
cat >/tmp/body.json <<'EOF'
{"inventory_item_id":123,"location_id":40052293765,"available":5}
EOF

HMAC=$(node -e "const fs=require('fs'),c=require('crypto');const b=fs.readFileSync('/tmp/body.json');console.log(c.createHmac('sha256', process.env.SHOPIFY_API_SECRET).update(b).digest('base64'))")

curl -i -X POST http://127.0.0.1:8000/webhooks/inventory-levels \
  -H "Content-Type: application/json" \
  -H "X-Shopify-Hmac-Sha256: $HMAC" \
  -H "X-Shopify-Topic: inventory_levels/update" \
  -H "X-Shopify-Shop-Domain: your-store.myshopify.com" \
  --data-binary @/tmp/body.json
```

---

## Deployment (Railway)

- **Start command:**  
  `uvicorn backend.app.main:app --host 0.0.0.0 --port $PORT`
- **Public Networking:** Must be enabled.
- **Environment variables:**  
  - `SHOP_URL`, `SHOPIFY_API_SECRET`, `SHOPIFY_ACCESS_TOKEN`, etc.
- **Health check:**  
  - `GET /health` returns `{"status":"ok"}`

---

## Implementation Notes

- **HMAC verification:**  
  - Always use the Shopify webhook secret (`SHOPIFY_API_SECRET`) for HMAC. The Gateway must forward the original bytes and `X-Shopify-Hmac-Sha256`.
- **Variant/product resolution:**  
  - REST `variants.json` is post-filtered; always falls back to GraphQL for accuracy.
- **Condition extraction:**  
  - Only variants with a `Condition` option of `Light`, `Moderate`, or `Heavy` are considered damaged.
- **Business rules:**  
  - Logic for publish/unpublish, canonical, and redirect is idempotent and only applies to damaged variants.
- **Persistence:**  
  - All events upsert to `damaged.inventory` via Supabase RPC.
- **Replay & reconcile:**  
  - Gateway replay and `/admin/reconcile` ensure eventual consistency and recovery from missed events.

---

## Troubleshooting

- **502 “Application failed to respond” (Railway):**  
  - Ensure service binds to `0.0.0.0:$PORT` and Public Networking is enabled.
- **401 “Invalid HMAC signature”:**  
  - Confirm the secret and that the body is the exact raw bytes from Shopify (no re-stringify).
- **400 “Missing HMAC header”:**  
  - Gateway must forward `X-Shopify-Hmac-Sha256`.
- **REST variant API returns unrelated/empty:**  
  - Service will fall back to GraphQL; check logs for fallback.
- **“Product is not a damaged book, skipping”:**  
  - Variant did not have a `Condition` option of `Light`, `Moderate`, or `Heavy`.
- **Replay HMAC mismatch:**  
  - Ensure Gateway forwards the raw Buffer, not a re-serialized body.
- **Duplicate deliveries:**  
  - DBS expects idempotency if `X-Gateway-Event-ID` is present.

---

## Roadmap

- Use `available_hint` for faster stock checks.
- Further harden `inventory_service` with Shopify GraphQL.
- Expand to additional Shopify webhook topics.
- Add structured logging and richer notifications.
- Batch pagination for scan-all.
- Add end-to-end integration tests.
- Optional Gateway HMAC verification.
- Support idempotency table keyed by `X-Gateway-Event-ID`.

---

## License

Private/internal.