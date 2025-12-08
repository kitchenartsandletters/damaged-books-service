# Damaged Books Service (DBS)

DBS is a FastAPI backend that powers Kitchen Arts & Letters’ **used/damaged book infrastructure**, including:

- Shopify webhook ingestion  
- Damaged‑inventory normalization in Supabase  
- Canonical‑SEO management  
- Publish/unpublish automation  
- Redirect rules  
- Bulk damaged‑product creation  
- Creation logging for Admin Dashboard  

DBS is designed to work together with the **Webhook Gateway**, which provides replay, HMAC verification, and delivery resilience.

---

# 1. High‑Level Architecture

### 🔄 Webhook Flow (Inventory → DBS → Shopify)
1. **Shopify emits** `inventory_levels/update`
2. **Gateway receives**, verifies HMAC, forwards raw body + all headers
3. DBS:
   - Verifies Shopify HMAC again (defense‑in‑depth)
   - Resolves the inventory item → variant → product via **GraphQL-first**
   - Hydrates product once (GraphQL) — no repeated fetches
   - Extracts damaged condition from `selectedOptions`
   - Upserts into Supabase (`damaged.inventory`)
   - Runs publish/unpublish rules
   - Resolves/writes canonical metafield
   - Ensures redirect correctness

### 🧱 Core Internal Services
- **shopify_client** — unified GraphQL-first Shopify admin API client  
- **inventory_service** — reconciliation, availability, variant condition mapping  
- **seo_service** — canonical resolution & metafield writes  
- **redirect_service** — create/remove Shopify redirects  
- **used_book_manager** — primary orchestrator for webhook + reconcile flows  
- **product_service** — bulk creation, duplicate checks, damaged-product creation  
- **creation_log_service** — structured logging of all bulk-creation events  

---

# 2. Supabase Schema (Damaged Inventory)

### Schema: `damaged`
- **Table:** `inventory` — authoritative inventory row per variant  
- **View:** `inventory_view` — adds computed fields, grouped status  
- **Function:** `damaged_upsert_inventory` — SECURITY DEFINER UPSERT  
- **Table:** `creation_log` — audit log of all damaged-product creation attempts  

`creation_log` fields include:
- id (PK)  
- canonical_handle  
- damaged_handle  
- result_status  
- product_id  
- operator  
- messages[]  
- timestamp  

---

# 3. Canonical SEO Strategy (Authoritative Specification)

Shopify **does not** store canonical URLs. The theme calculates `canonical_url` dynamically.  
Therefore DBS uses **metafield redirection**:

- Every damaged product receives metafield:  
  ```
  namespace: custom  
  key: canonical_handle  
  value: <canonical product.handle>  
  ```
- Theme override pattern:

```
{% if product.metafields.custom.canonical_handle %}
  <link rel="canonical" href="{{ routes.root_url }}/products/{{ product.metafields.custom.canonical_handle }}">
{% else %}
  <link rel="canonical" href="{{ canonical_url }}">
{% endif %}
```

DBS ensures this metafield is kept correct during:
- webhook processing  
- reconcile  
- bulk creation  

---

# 4. Damaged Product Creation System (Authoritative Rules)

This is the **canonical specification** for how all damaged products are created.  
It applies to:
- Admin bulk-creation wizard  
- Admin single-item creation  
- Programmatic creation  

## 4.1 Derivation Rules

### 📘 Title
```
<canonical base title>: Damaged
```
Base title = canonical title trimmed at first `: ; – — -`.

### 🏷 Handle
```
<canonical.handle>-damaged
```
No trimming or punctuation removal.  
This rule is **absolute**.

### 🧾 Body HTML (Fixed Copy)
Exact required text:

```
We received some copies of this book which are less than perfect...
[full block unchanged]
```

### Other inherited fields:
- vendor  
- product_type  
- canonical SKU  
- first image only  
- canonical tags **+ "damaged"**  
- SEO title = canonical_title + " (Damaged)"  
- SEO description = fixed damaged preamble  

### Not inherited:
- Collections  
- SEO description from canonical  
- Weight variations  

---

## 4.2 Variant-Level Rules

Each damaged product always contains **three variants**:

| Condition | Default Discount | Title | Inventory Policy | Barcode | SKU |
|----------|------------------|--------|------------------|---------|-----|
| light    | 15% off          | Light Damage | deny | `<snake(canonical_handle)>_light` | canonical |
| moderate | 30% off          | Moderate Damage | deny | `<snake>_moderate` | canonical |
| heavy    | 60% off          | Heavy Damage | deny | `<snake>_heavy` | canonical |

### Synthetic barcodes (DBS 2025‑v2 rule)
```
snake_case(canonical_handle) + "_" + condition_key
```

### VariantSeed overrides
Wizard may supply:
```
condition: "light" | "moderate" | "heavy"
quantity: int
price_override: float  # percentage off (0.25 = 25% off)
```

**compare_at_price is ignored for now.**

Quantities are **reported**, but not yet applied to Shopify inventory.

---

# 5. Duplicate-Prevention & Bulk Creation (2025 Initiative)

DBS now provides a full duplicate-guarded product‑creation pipeline.

---

## 5.1 Request Models (Updated)

### BulkCreateRequest
```
{
  canonical_handle: str,
  canonical_title: str,
  isbn: str | null,
  barcode: str | null,
  variants: [VariantSeed] | null,
  dry_run: bool
}
```

### Removed:
❌ damaged_title  
❌ damaged_handle  
These are now always **derived automatically**.

---

## 5.2 Duplicate Rules (Strict)

A canonical_handle is considered conflicting if:

- canonical product not found (forbidden)  
- damaged handle already exists (or suffixed version exists)  
- Supabase has inventory rows for same root-damaged handle  
- Shopify has auto-suffix collisions  

Wizard must correct conflicts before proceeding.

---

## 5.3 Admin Endpoints (Updated)

### `POST /admin/check-duplicate`
Input: canonical metadata only  
Output: conflict analysis, derived damaged handle, safety flag

### `POST /admin/bulk-preview`
Per-entry:
- derive damaged handle  
- run duplicate check  
- report conflicts  

### `POST /admin/bulk-create`
For each safe entry:
- derive titles/handles  
- run duplicate guard again  
- create damaged product via `create_damaged_pair()`  
- record result in creation_log  

---

# 6. product_service — Final Behavior Summary

### `create_damaged_pair()`
- Fetch canonical product  
- Derive damaged title + handle  
- Compute prices / overrides  
- Build 3 variants  
- Assign synthetic barcodes  
- Set metafield `custom.canonical_handle`  
- Create damaged product (Shopify REST)  
- Return structured product info  

### `create_damaged_product_with_duplicate_check()`
- Run duplicate check  
- Dry-run support  
- Call create_damaged_pair  
- Extract variant info  
- Write creation_log entry  

### `check_damaged_duplicate()`
- Shopify canonical search (with suffix fallbacks)  
- Shopify damaged search  
- Supabase inventory collision check  
- Returns conflict map + safe_to_create  

---

# 7. Creation Log Subsystem

### Purpose
Record **every** creation attempt for operator auditing and Dashboard UI.

### Logged fields (summarized)
- operator (optional)  
- canonical_handle  
- damaged_handle  
- result_status (`error`, `dry-run`, `created`)  
- variant summaries  
- messages[]  
- product_id  
- created_at timestamp  

### Endpoint
```
GET /admin/creation-log
```

---

# 8. Reconcile & Replay Pipeline

### `/admin/reconcile`
- Iterate inventory_view  
- Fetch Shopify variant via GraphQL  
- Preserve existing condition if Shopify omits it  
- Upsert  
- Reapply publish/unpublish + redirect rules  

### Replay (Gateway)
- Replays raw webhook  
- DBS treats it as a new event  
- HMAC restored, logic identical  

---

# 9. Endpoints Summary

(keep existing list from README, updated for new Admin endpoints:  
check-duplicate, bulk-preview, bulk-create, creation-log)

---

# 10. Example curl for Bulk Creation

### Preview (no mutations)
```
curl -X POST "$DBS/admin/bulk-preview" -H "X-Admin-Token:$TOKEN" -d '{
  "entries": [
    {
      "canonical_handle": "the-last-unicorn",
      "canonical_title": "The Last Unicorn",
      "isbn": "9780999999999",
      "barcode": "9780999999999",
      "variants": [
        {"condition": "light", "price_override": 0.25, "quantity": 3},
        {"condition": "moderate", "quantity": 1}
      ]
    }
  ]
}'
```

### Create
```
curl -X POST "$DBS/admin/bulk-create" -H "X-Admin-Token:$TOKEN" -d '{
  "entries": [...]
}'
```

---

# 11. E2E Testing Guide (Staging)

### Pre-flight
- Ensure Supabase migration for `damaged.creation_log` applied  
- Ensure Shopify staging store accessible  
- Ensure Webhook Gateway is live  

### Test Steps
1. Choose canonical product:
   - handle: `the-last-unicorn`
   - price: e.g. 40.00
2. Run bulk-preview  
3. Run bulk-create  
4. Verify:
   - Shopify product exists with correct title, handle, variants, prices  
   - Synthetic barcodes correct  
   - Supabase `inventory_view` empty (starts at 0 stock)  
   - creation_log contains entry  
   - Redirect NOT created (draft product)  
5. Perform an inventory webhook:
   - Set Light Damage stock to >0  
6. Confirm:
   - Product is published  
   - Redirect removed  
   - canonical_handle metafield exists  

---

# 12. Deployment

(keep existing section)

---

# License

Private/internal.