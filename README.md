# Damaged Books Service (DBS)

The Damaged Books Service (DBS) is a FastAPI backend that processes Shopify inventory level webhooks for used/damaged book variants, applies business rules (publish/unpublish, SEO canonical, redirects), and persists a normalized view of damaged inventory in Supabase. It is designed to be driven by the Webhook Gateway, which relays Shopify webhooks with original headers and body for robust HMAC verification and replay support.

---

## System Overview

### Modern Flow
1. **Webhook ingestion:** Shopify emits `inventory_levels/update` → Gateway receives, verifies Shopify HMAC, and forwards the raw POST (body + headers) to DBS `/webhooks/inventory-levels`.
2. **HMAC verification:** DBS verifies the Shopify HMAC signature (defense-in-depth).
3. **Variant & product resolution (GraphQL‑first):**
   - Use `shopify_client.get_variant_product_by_inventory_item(inventory_item_id)` to map the inventory item to `variant_id`, `product_id`, and `handle` in a single GraphQL call.
   - If (and only if) GraphQL returns no match, fall back to REST `GET /admin/variants.json?inventory_item_ids=...` (post‑filtered for exact `inventory_item_id`).
   - This logic is centralized in `shopify_client.resolve_inventory_item(...)` and consumed by the route layer.
4. **Product hydration (GraphQL):**
   - Fetch the product once via `shopify_client.get_product_by_id_gql(product_id)` and pass it through to the manager. The manager **trusts the passed product** and does not re-fetch it.
5. **Canonical resolution & write (once per event):**
   - `seo_service.resolve_canonical_handle(damaged_handle, product)` resolves the canonical destination (strip `-damaged`, honor redirects if present, otherwise trust the stripped handle).
   - `seo_service.update_used_book_canonicals(product, canonical_handle)` writes `metafields.custom.canonical_handle` exactly once per webhook event.
6. **Condition extraction & persistence:**
   - Query Shopify GraphQL variant `selectedOptions` to extract `Condition` (Light/Moderate/Heavy).
   - Preserve both `condition_raw` (human value) and `condition_key` (normalized snake_case) and upsert via Supabase RPC `damaged_upsert_inventory`.
7. **Business rules (idempotent):**
   - **Publish/unpublish** damaged product via GraphQL `productUpdate` using `shopify_client.set_product_publish_status(product_id, publish=True|False)`.
   - **Redirects:** If **ANY** variant in stock → publish and **remove** redirect if present. If **ALL** variants out of stock → unpublish and **ensure** redirect exists from damaged handle to canonical handle. Aggregate availability comes from `damaged.inventory_view`.
8. **Response:** Always returns HTTP 200 on internal errors (to avoid Shopify retries), 401 only for HMAC failures.

---

## Architecture Updates

- **GraphQL‑first resolution:** Inventory item → (variant_id, product_id, handle) is now resolved via GraphQL first, with REST as a rare fallback. Centralized in `shopify_client.resolve_inventory_item`.
- **Single product hydration:** The route hydrates product via GraphQL and passes it forward. `used_book_manager` trusts the object and does not re‑fetch.
- **Single canonical write:** `seo_service` resolves the canonical once and writes `custom.canonical_handle` exactly once per event.
- **Unified publish/unpublish:** Moved from `product_service` to `shopify_client.set_product_publish_status` (GraphQL `productUpdate`).
- **Redirects rule clarified:** Only create redirect when **all** damaged variants are out of stock; remove it when **any** variant returns to stock.
- **Removed duplication:** Eliminated duplicate `variants.json` calls and duplicated canonical writes.
- **Deprecation:** `api/webhooks.py` inventory route removed; `/webhooks/inventory-levels` lives in `routes.py`.

---

## Key Services and Responsibilities

- **shopify_client:** Centralized Shopify Admin API access (GraphQL‑first). Provides:
  - `get_variant_product_by_inventory_item(inventory_item_id)`
  - `get_product_by_id_gql(product_id)`
  - `set_product_publish_status(product_id, publish: bool)`
  - `resolve_inventory_item(inventory_item_id)` — unified helper that uses GraphQL first with REST fallback.
- **inventory_service:** Uses Shopify GraphQL to check stock and map `selectedOptions` (Condition). Reconcile also calls through GraphQL.
- **used_book_manager:** Orchestrates the flow. **Trusts the hydrated product passed in** (no additional product GETs), extracts condition, upserts to Supabase, runs publish/unpublish + redirect rules, and triggers canonical write exactly once per event.
- **seo_service:** Resolves canonical handle (strip `-damaged`, check redirect, else trust stripped handle) and writes `metafields.custom.canonical_handle` one time per webhook event.
- **redirect_service:** Finds, creates, and deletes redirects between damaged and canonical handles.
- **product_service:** **Deprecated**. All publish/unpublish is handled by `shopify_client.set_product_publish_status(...)`.

---

## Supabase Schema

- **Schema:** `damaged`
- **Table:** `damaged.inventory` (PK: `inventory_item_id`)
- **View:** `damaged.inventory_view` (adds computed stock status and joins)
- **Changelog:** `damaged.changelog` (optional/future)
- **Upsert Function:** `damaged.damaged_upsert_inventory(...)` (SECURITY DEFINER, sets `search_path` to `public, damaged`)  
  - Expects both `condition_raw` and `condition_key` to be provided and preserved.

---

## Damaged-Book Detection (Current)

- **Primary classification:** Product **handle** ending with `-damaged` marks the item as a damaged/used product.
- **Condition reporting:** Variant `selectedOptions` (via GraphQL) are still read to extract the human value (`condition_raw`, e.g., "Light Damage") and a normalized key (`condition_key`, e.g., "light_damage") for analytics and display.
- **Notes:** This approach ensures fast, unambiguous classification without relying on tags. We may consider an options‑only detection in the future, but the handle suffix has proven reliable with our catalog structure.

---

## Endpoints

- `GET /health` — Health check. Returns `{"status":"ok"}`
- `POST /webhooks/inventory-levels` — Main webhook endpoint. Expects Shopify HMAC, original body, and headers (via Gateway).
  - Implemented in `routes.py` (the legacy `api/webhooks.py` route has been removed).
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
- **Reconcile:** Admin endpoint `/admin/reconcile` walks all rows in `damaged.inventory_view`, calls `resolve_by_inventory_item_id` from `inventory_service` to fetch current Shopify inventory via GraphQL, including both availability and condition info, preserving both fields. If Shopify omits condition or availability, existing DB values are preserved. The pipeline then upserts the latest status via `damaged_upsert_inventory`. This ensures DBS/Supabase state matches Shopify reality (even if webhooks were dropped).
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
- **Variant/product resolution:** GraphQL‑first using `shopify_client.resolve_inventory_item(...)` (REST fallback only if GraphQL yields no match). This removed duplicate REST calls and simplified the route.
- **Condition extraction:**  
  - Only variants with a `Condition` option of `Light`, `Moderate`, or `Heavy` are considered damaged.
  - Both `condition_raw` (human-readable) and `condition_key` (snake-cased normalized) are extracted and persisted.
- **Business rules:** Publish/unpublish via GraphQL `productUpdate` using `shopify_client.set_product_publish_status(...)`. Canonical is resolved/written **once** per event in `seo_service`. Redirect creation/removal follows the aggregate stock rule (ALL OOS → create; ANY in stock → remove).
- **Persistence:**  
  - All events upsert to `damaged.inventory` via Supabase RPC, preserving both condition fields.
- **Replay & reconcile:**  
  - Gateway replay and `/admin/reconcile` ensure eventual consistency and recovery from missed events.
  - Reconcile now uses Shopify GraphQL directly for availability and conditions, aligning with webhook processing, via `resolve_by_inventory_item_id`.
- **Reconcile fallback:**  
  - If Shopify omits condition or availability data during reconcile, existing database values are preserved.

---

## Future Optimizations

- **Per-event redirect lookup cache:** Propose a patch to cache `redirect_service.find_redirect_by_path(handle)` results within a single webhook processing pass (per-handle memoization) so we don’t hit `/redirects.json` twice for the same handle (e.g., `test-book-title`). This is a low-risk latency and rate-limit optimization.

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

## Bulk Creation & Duplicate‑Prevention Roadmap (2025 Phase)

This section documents the new Bulk Damaged‑Book Creation Wizard initiative and the supporting backend infrastructure. It summarizes **what has been completed** and **what remains open**, organized so the README can serve as the project tracker for the next development phase.

---

### ✅ Completed (Phase 1)

**Backend foundations**
- Added `product_service.check_damaged_duplicate()` for pre‑creation duplicate detection.
- Added Admin API routes:
  - `POST /admin/check-duplicate` (single product duplicate guard)
  - `POST /admin/bulk-preview` (multi-product preflight with conflict reporting)
- Implemented handle normalization across preview endpoints.
- Consolidated condition extraction mapping into `inventory_service` to prevent double-mapping.
- Implemented safe quantity‑coercion (`int(float(x))`) within `inventory_service` and `used_book_manager`.
- Fixed redirect‑coroutine bug in `seo_service.resolve_canonical_handle`.
- Added Shopify rate‑limit hardening (memoized redirect lookups + reduced calls).
- Established the architectural contract for **canonical metafield strategy** and tested consistent canonical writes.

**Operational improvements**
- Prevent Shopify suffix pollution (`-1`, `-2`) by introducing pre‑creation duplicate guards.
- Added logging around variant→product resolution and Supabase upserts.
- Reconcile is now fully GraphQL‑based and aligned with webhook logic.

---

### 🚧 In Progress (Phase 2 — Core Creation Engine)

These items are required to safely create canonical/damaged product pairs programmatically:

1. `product_service.create_damaged_pair()` — **Full implementation needed**
   - Create canonical product (draft)
   - Create damaged product (active, 3 condition variants)
   - Return structured canonical/damaged metadata to caller

2. Strengthen duplicate‑prevention logic
   - Prevent duplicate Shopify handles pre‑creation
   - Prevent duplicates in Supabase `inventory` table
   - Provide structured conflict explanations for UI

3. Finalize schemas for the Dashboard wizard (request + response)
   - `BulkDuplicateCheckRequest`
   - `BulkPreviewResponse`
   - `BulkCreateResponse`
   - Conflict objects with standardized reasons/codes

4. Finish `/admin/bulk-create`
   - Add dry‑run support
   - Add structured results mapping each input to creation success/failure
   - Integrate Supabase logging (see next phase)

---

### 🗂️ Phase 3 — Dashboard Wizard UI (Upcoming)

1. **Preflight Screen**
   - Upload/enter batch of titles/handles
   - Call `/admin/bulk-preview`
   - Display conflict resolution UI

2. **Conflict Resolution Modal**
   - Edit canonical/damaged handles
   - Remove items from batch
   - Re-run preview step

3. **Confirmation Step**
   - Summary table of creations
   - Operator confirmation required before finalizing
   - POST `/admin/bulk-create`

4. **Post‑Creation Summary**
   - Show Shopify links
   - Show Supabase log IDs
   - Highlight any partially created entries

---

### 🗄️ Phase 4 — Logging & Observability (Upcoming)

- Create Supabase table: `damaged.creation_log`
- Record:
  - operator ID
  - canonical/damaged handles
  - product IDs
  - timestamps
  - notes (warnings, conflicts, resolutions)
- Add API endpoint: `GET /admin/creation-log`
- Add Dashboard viewer page

---

### 🧪 Optional Enhancements (Future)
- “Quick Add” single‑item modal in Dashboard
- Shopify catalog selector to auto-fill canonicals
- Full dry-run simulator using abstracted Shopify client
- Pre‑publishing GID caching to reduce Shopify calls
- Idempotency for bulk operations using UUID batch keys

---

This roadmap is the authoritative checklist for the Bulk Creation Initiative and accompanies the existing DBS architecture sections above.


## Canonical URLs

Short version:
	•	#1: product.seo is only title + meta description. There is no per-product canonical URL field in the Admin API. Shopify’s own docs confirm that Product.seo contains just an SEO title and description.  ￼
	•	#2: The <link rel="canonical" …> tag you see in your theme comes from the Liquid global canonical_url. It’s computed at render time by Shopify, not something you “store” or can set via API. Shopify’s theme docs show exactly this pattern and state that canonical_url is provided by Shopify.  ￼
	•	#3: Because of #1 and #2, you can’t “set” canonicals through product.seo. To control the canonical target for damaged products, you must override what the theme outputs—typically by checking a product metafield and, if present, outputting your own canonical link (otherwise fall back to {{ canonical_url }}).

So to answer your exact questions:
	•	“Where is canonical_url stored?”
It isn’t stored per product. It’s a Liquid global that Shopify calculates for the current page and exposes to themes. You can use it, but you can’t update it through the Admin API.  ￼
	•	“Does canonical_handle need to be canonical_url to match theme.liquid?”
No. Your theme can continue to reference canonical_url by default, but you should conditionally override it when our app has resolved a better canonical. The usual pattern is to store a metafield (e.g., custom.canonical_handle) on the damaged product and have the theme emit <link rel="canonical" href="..."> using that metafield; if it’s not set, fall back to {{ canonical_url }}. (Metafields are first-class content the theme can read.)  ￼

⸻

What we should implement
	1.	Our app (already in progress):
	•	Resolve the canonical handle for each damaged product.
	•	Write that handle into a product metafield (e.g., namespace custom, key canonical_handle).
	•	Keep it fresh during inventory webhooks and reconcile jobs.
	2.	Theme update (one-time):
In theme.liquid (or your head partial), replace the canonical tag with:

{% if template.name == 'product' and product %}
  {% if product.metafields.custom.canonical_handle %}
    <link rel="canonical"
          href="{{ routes.root_url }}/products/{{ product.metafields.custom.canonical_handle | escape }}">
  {% else %}
    <link rel="canonical" href="{{ canonical_url }}">
  {% endif %}
{% else %}
  <link rel="canonical" href="{{ canonical_url }}">
{% endif %}

Our app writes `custom.canonical_handle` exactly once per event so the theme only evaluates it and does not need to manage timing or retries.

	3.	Why this is SEO-correct:
Google’s guidance is to use one canonical URL for duplicate/near-duplicate content. By always canonicalizing damaged SKUs to the undamaged primary page, you consolidate signals and avoid needless indexing of transient damaged pages.  ￼

⸻

TL;DR
	•	You can’t set canonicals via product.seo; it’s only title/description.  ￼
	•	canonical_url is a computed Liquid value, not stored per product.  ￼
	•	The right approach is: app writes a canonical handle metafield on damaged products → theme conditionally emits a canonical tag using that metafield, otherwise falls back to {{ canonical_url }}. This gives us precise, programmatic control over SEO behavior without relying on manual edits.

---

## License

Private/internal.