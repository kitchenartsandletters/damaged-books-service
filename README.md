# Damaged Books Service (DBS)
**Kitchen Arts & Letters – Used & Damaged Books Infrastructure**

DBS is the backend that powers the full lifecycle of **used/damaged books** across Shopify, Supabase, and internal Admin tooling. It provides:

- Shopify webhook ingestion (inventory + products)
- Damaged-inventory normalization in Supabase
- Canonical SEO + metafield synchronization
- Automatic publish/unpublish rules
- Automatic redirect management
- Bulk damaged-product creation (duplicate-safe)
- Variant-level initialization of inventory
- Full creation logging for auditing and Dashboard UI

DBS works together with the **Webhook Gateway**, which ensures replayability, HMAC safety, and delivery guarantees.

---

# 1. High-Level Architecture

## 🔄 Webhook Flow (Inventory → Gateway → DBS → Supabase → Shopify)
1. Shopify sends `inventory_levels/update`
2. Gateway verifies HMAC, forwards raw body + headers
3. DBS:
   - Re-verifies HMAC (defense-in-depth)
   - Resolves variant + product using **GraphQL-first**  
   - Hydrates product once (no double fetches)
   - Extracts damaged condition from `selectedOptions`
   - Normalizes condition (`light`, `moderate`, `heavy`)
   - Upserts row into `damaged.inventory`
   - Applies publish/unpublish rules
   - Ensures canonical metafield correctness
   - Updates/cleans redirect rules

The system is **idempotent**: replays and duplicate webhook bursts are safe.

---

# 2. Supabase Schema — `damaged`

### Core Tables & Functions

| Name | Type | Purpose |
|------|------|---------|
| `inventory` | table | One authoritative row per damaged variant |
| `inventory_view` | view | Joins metadata + condition + product info |
| `creation_log` | table | Audit log of all product-creation attempts |
| `damaged_upsert_inventory` | function | SECURITY DEFINER UPSERT |

### Notes
- Inventory is **only** written via the UPSERT function.
- `creation_log` captures all success/error/dry-run events.

---

# 3. Canonical SEO Strategy

DBS enforces canonical accuracy with a metafield:

```
namespace: custom
key: canonical_handle
value: <canonical.handle>
```

Themes override:

```liquid
{% if product.metafields.custom.canonical_handle %}
  <link rel="canonical" href="{{ routes.root_url }}/products/{{ product.metafields.custom.canonical_handle }}">
{% else %}
  <link rel="canonical" href="{{ canonical_url }}">
{% endif %}
```

Redirects are automatically added/removed to maintain correctness.

---

# 4. Damaged Product Creation — 2025 Authoritative Specification

Applies to API, wizard, and programmatic creation.

## 4.1 Title, Handle, Body

**Title**
```
<canonical base title>: Damaged
```

**Handle**
```
<canonical.handle>-damaged
```

**Body HTML**
A fixed block of damaged-book explanation (preserved in code).

**Inherited**
- vendor  
- product_type  
- author SKU  
- first image  
- canonical tags + `"damaged"`  
- SEO title: `<canonical> (Damaged)`  
- SEO description: damaged preamble  

**Not inherited**
- Collections  
- Weight  

---

# 4.2 Variant Rules (Always 3 variants)

| Condition | Default Discount | Title | Policy | Barcode | SKU |
|----------|------------------|--------|--------|---------|-----|
| light    | 15%              | Light Damage | deny | `<snake>_light` | canonical |
| moderate | 30%              | Moderate Damage | deny | `<snake>_moderate` | canonical |
| heavy    | 60%              | Heavy Damage | deny | `<snake>_heavy` | canonical |

Synthetic barcodes follow:

```
snake_case(canonical_handle) + "_" + condition_key
```

**VariantSeed support**
```
condition, quantity, price_override
```

---

# 5. Inventory Initialization (Phase 2 – 2025-v3)

After product creation:

1. GraphQL fetch variant → get `inventory_item_id`
2. Set Shopify inventory via `inventory_levels/set.json`
3. Shopify emits webhook
4. DBS upserts into Supabase
5. Publish Rule:
   - If **any** variant has available > 0 → product is **published**
   - If all variants = 0 → product becomes **unpublished**

This sensitivity is **intentional**.

## 5.1 Shopify Theme Integration (PDP Support for Damaged Books)

The Damaged Books Service exposes variant-level condition identity (`light`, `moderate`, `heavy`) and ensures consistent barcoding, availability, and product publication rules. To surface this in the storefront, lightweight Shopify theme customizations provide:

- Dynamic condition descriptions on damaged PDPs  
- Full compatibility with variant selection (`variant-radios`)  
- Stable behavior for all non-damaged products  
- Zero interference with existing preorder, backorder, or event logic  

These changes live entirely in Liquid plus a small JavaScript asset and require no build tools.

### 5.1.1 Dynamic Damage-Condition Block

On any product whose handle contains `-damaged`, the PDP injects:

```liquid
<div id="damage-condition-dynamic"
     class="damage-condition-block"
     style="display:none; margin-bottom: 1.5rem;"></div>
```

A JSON map of condition-to-description is provided via a snippet:

```liquid
<script id="damage-copy-json" type="application/json">
{
  "Light Damage": "...",
  "Moderate Damage": "...",
  "Heavy Damage": "..."
}
</script>
```

These descriptions match the internal KAL classification system and DBS condition rules.

### 5.1.2 Behavior (Variant-Driven)

When the customer selects a variant, the dynamic block updates:

- `Light Damage` → light explanation  
- `Moderate Damage` → moderate explanation  
- `Heavy Damage` → heavy explanation  
- Any non-damaged variant → hidden  

The logic relies on the theme’s global `product:variant-change` event, which provides `event.detail.variant`.

### 5.1.3 JavaScript: `damage-condition-dynamic.js`

```javascript
(function () {
  const conditionCopyEl = document.getElementById("damage-copy-json");
  if (!conditionCopyEl) return;

  let conditionCopy = {};
  try {
    conditionCopy = JSON.parse(conditionCopyEl.textContent || "{}");
  } catch (e) {
    console.warn("Couldn't parse damage-condition JSON", e);
  }

  function updateDamageBlock(variant) {
    const block = document.getElementById("damage-condition-dynamic");
    if (!block) return;

    const title = variant?.public_title;
    if (!title || !conditionCopy[title]) {
      block.style.display = "none";
      block.innerHTML = "";
      return;
    }

    block.innerHTML = conditionCopy[title];
    block.style.display = "block";
  }

  document.addEventListener("product:variant-change", (event) => {
    updateDamageBlock(event.detail.variant);
  });

  window.addEventListener("load", () => {
    const el = document.querySelector("variant-radios script[type='application/json']");
    if (!el) return;

    try {
      const variants = JSON.parse(el.textContent || "[]");
      const firstVariant = variants.find(
        v => v.id == ShopifyAnalytics.meta.selectedVariantId
      );
      if (firstVariant) updateDamageBlock(firstVariant);
    } catch (e) {
      console.warn("Damage initialization error:", e);
    }
  });
})();
```

### 5.1.4 Stock Messaging Compatibility

The PDP contains complex logic for preorders, out-of-print offers, Events, Hats/T-Shirts, GCP, and general books.  
A Liquid normalization block ensures variant-level inventory (coming from DBS) produces correct messages without interfering with specialized product types.

Rules:

| Product Type | Behavior |
|--------------|----------|
| Damaged Books | Standard in-stock/backorder rules; description block handles UX |
| Preorder | Always shows preorder message |
| Past Out-of-Print | Always shows OOP message |
| Hats/T-Shirts | Always show custom merch message |
| Event | Sold-out → “event ended”; otherwise normal |
| GCP | Never backordered |
| General Books | Standard messaging |

### 5.1.5 Safety Guarantees

- Theme JS never blocks product-form behavior  
- PDP remains stable if JSON missing or variant event fails  
- No third-party dependencies  
- Asset loads with `defer` to avoid render blocking  

### 5.1.6 Why This Matters

These theme enhancements complete the damaged-books pipeline:

1. DBS normalizes condition + inventory  
2. DBS controls publication state  
3. Theme renders variant-specific UX  
4. Gateway keeps Supabase synchronized  
5. PDP always reflects the true state  

This produces a self-healing, automated damaged-books experience with zero manual intervention.

---

# 6. Duplicate Prevention

A canonical handle is rejected when:
- canonical product missing  
- damaged handle exists  
- Shopify has suffixed collisions  
- Supabase has inventory for this damaged root  
- canonical/damaged collisions occur  

Checks run on:
- `/admin/check-duplicate`
- `/admin/bulk-create/preview`
- `/admin/bulk-create` (final guard)

The preview endpoint (`/admin/bulk-create/preview`) returns duplicate conflicts but does not block confirm logic enforcement. All duplicate enforcement is ultimately handled at the confirm step.

---

# 7. Bulk Creation System

## Request Model
```
{
  inputs: [
    { type: "isbn" | "product_id", value: string }
  ],
  inventory: {
    light: number,
    moderate: number,
    heavy: number
  }
}
```

`canonical_handle` is deprecated and supported only for legacy testing. All resolution of input values happens server-side inside DBS.

## Endpoints
- `POST /admin/check-duplicate`
- `POST /admin/bulk-create/preview`
- `POST /admin/bulk-create`
- `GET  /admin/creation-log`

`POST /admin/bulk-create/preview` performs server-side resolution and validation only (no writes, no inventory mutation, and no logging side effects).

`/admin/bulk-create` is the confirm-only, irreversible step that performs creation and all side effects.

---

### 7.1 Preview → Confirm Contract (Authoritative)

The Bulk Create system is a strict two-phase workflow:

1. **Preview phase** (`POST /admin/bulk-create/preview`)
   - Resolves all inputs (ISBN or product_id) server-side
   - Enforces single-variant canonical rule
   - Detects duplicates and conflicts
   - Computes variant-level pricing and discounts
   - Returns a preview payload describing *exactly* what would be created
   - Performs **zero writes**

2. **Confirm phase** (`POST /admin/bulk-create`)
   - Accepts **only preview-derived items**
   - Performs irreversible Shopify writes
   - Re-runs duplicate checks as a final guard
   - Logs all outcomes in `creation_log`

The confirm endpoint does **not** re-resolve inputs, recompute inventory, or infer pricing.

### 7.2 BulkCreateConfirmRequest (Confirm-Only Schema)

The confirm endpoint accepts a restricted payload derived *only* from preview results:

```json
{
  "items": [
    {
      "canonical_handle": "string",
      "inventory": {
        "light": number,
        "moderate": number,
        "heavy": number
      }
    }
  ]
}
```

Guarantees:
- `items[]` must be non-empty
- Each `canonical_handle` must match a previewed product
- Inventory keys must be exactly: `light`, `moderate`, `heavy`
- Pricing, discounts, SKUs, and barcodes are **not** accepted from clients
- Any deviation results in a hard error

### 7.3 Admin Dashboard Integration (Handoff Notes)

The Admin Dashboard (AD) must treat DBS as an authoritative backend, not a helper API.

Rules:
- AD never resolves ISBNs or product IDs itself
- AD never computes pricing or discounts
- AD never mutates preview data
- AD confirm payloads must be derived directly from preview results

Required endpoints:
- `POST /admin/bulk-create/preview`
- `POST /admin/bulk-create` (confirm-only)

The Bulk Create Wizard in AD mirrors this contract:
- Preview = truth
- Confirm = irreversible execution

# 8. Creation Log

Logs every creation attempt:

- status: `error`, `dry-run`, `created`
- canonical_handle  
- damaged_handle  
- product_id  
- variants summary  
- messages  
- timestamp  

Logging failures never break creation.

---

# 9. Reconcile Flow

`/admin/reconcile`:
- iterates Supabase inventory_view  
- fetches Shopify variant  
- normalizes condition  
- upserts  
- reapplies publication + metafields + redirects  

A full “repair + normalize” operation.

---

# 10. E2E Testing

1. Bulk-create a damaged product  
2. Verify Shopify data (title, handle, variants, barcodes, prices)  
3. Verify Supabase (`inventory_view`)  
4. Verify creation_log  
5. Modify inventory → trigger webhook  
6. Confirm:
   - Supabase updated  
   - Product publishes/unpublishes correctly  
   - Redirects & canonical metafields correct  

---

# 11. Deployment
(Existing deployment instructions unchanged.)

⸻

## Future Development: Collection Publishing Strategies (Deferred)

The Damaged Books Service (DBS) currently manages publication state reactively, driven by Shopify inventory webhooks. This design is intentional and correct for the current operational model.

However, there are scenarios where additional collection-level automation may be desirable in a future phase. The following options are documented here for reference only and are not implemented.

Current Behavior (Authoritative)
	•	Damaged products are created without forced collection membership.
	•	Inventory changes (via inventory_levels/update) trigger DBS evaluation.
	•	Publication rules:
	•	If any damaged variant has available inventory → product is published.
	•	If all variants reach zero → product is unpublished.
	•	Collection membership (e.g. damaged-books) is therefore implicitly managed by publication state and webhook activity.

This keeps DBS idempotent, replay-safe, and aligned with Shopify’s source of truth.

⸻

Option A — Inventory-Only Webhook Control (Status Quo)

Description
Continue relying exclusively on inventory webhooks to drive both product publication and collection membership.

Pros
	•	Zero additional logic
	•	Fully event-driven and replay-safe
	•	No risk of drift between Supabase and Shopify

Cons
	•	Products with no inventory changes will not re-evaluate collection membership
	•	Bulk-created or manually edited products may require an inventory touch to reconcile

Status
✅ Current behavior
🟡 Documented for completeness

⸻

Option B — Post-Creation Reconciliation Hook (Recommended Future Option)

Description
After bulk creation or admin-driven damaged product creation, explicitly enqueue a reconciliation step that:
	•	Evaluates damaged variants
	•	Applies publish/unpublish rules
	•	Applies or removes the damaged-books collection membership

This would reuse existing reconciliation logic rather than duplicating rules.

Pros
	•	Ensures newly created products immediately reflect correct state
	•	No dependency on a future inventory webhook
	•	Minimal architectural change

Cons
	•	Adds one additional execution path
	•	Requires explicit invocation after creation workflows

Likely Location
	•	used_book_manager.py
	•	Or a shared reconciliation helper invoked by both webhooks and admin actions

Status
🔒 Deferred
⭐ Preferred next-phase enhancement

⸻

Option C — Scheduled Reconciliation / Safety Net

Description
Introduce a scheduled job (cron or manual admin endpoint) that periodically reconciles:
	•	Damaged inventory
	•	Publication state
	•	Collection membership

This acts as a safety net rather than a primary driver.

Pros
	•	Self-healing for rare edge cases
	•	Useful for audits and recovery scenarios

Cons
	•	Not real-time
	•	Additional operational surface area

Status
🔒 Deferred
🟡 Optional hardening step

⸻

Explicit Non-Goals (For Now)
	•	No collection toggling directly in Liquid
	•	No polling-based Shopify queries
	•	No nav-menu mutation without authoritative backend state
	•	No deviation from webhook-first design

⸻

Summary

The current DBS behavior is correct and intentional.
These options are documented to prevent future ambiguity and to provide a clear, scoped roadmap when requirements change.

No action is required unless:
	•	Bulk creation becomes frequent without inventory movement, or
	•	Operational tooling requires immediate visibility guarantees.

# 12. Manual Testing: cURL Bulk-Create Example

**Note:** This legacy cURL example uses the deprecated `canonical_handle` path. Bulk workflows must now use `inputs[]` plus the preview → confirm flow.

This section documents the **canonical test request** used to validate damaged-product creation end‑to‑end against the live Shopify catalog.

This is intended for **engineering verification only**.  
Staff should **never** use this method in day‑to‑day operations.

---

## 12.1 Purpose of This Test

This cURL request verifies that:

- Duplicate checks are enforced
- A damaged product is created with the correct handle and title
- All three damage-condition variants are created
- Variant-level inventory is initialized correctly
- Creation is logged in `creation_log`
- Publish / unpublish rules are evaluated correctly

It exercises the **entire DBS pipeline**.

---

## 12.2 Example Payload

Example JSON payload used during testing:

```json
{
  "canonical_handle": "the-irish-bakery",
  "variants": [
    { "condition": "light", "quantity": 12 },
    { "condition": "moderate", "quantity": 0 },
    { "condition": "heavy", "quantity": 0 }
  ],
  "dry_run": false
}
```

Notes:
- `canonical_handle` must refer to an **existing canonical product**
- `condition` must be one of: `light`, `moderate`, `heavy`
- Quantity may be zero for any variant
- `dry_run: false` performs a real creation

---

## 12.3 cURL Command

```bash
curl -X POST https://used-books-service-production.up.railway.app/admin/bulk-create \
  -H "Content-Type: application/json" \
  -H "x-admin-token: $VITE_DBS_ADMIN_TOKEN" \
  -d @the-irish-bakery.json
```

Where:
- `the-irish-bakery.json` contains the payload shown above
- `$VITE_DBS_ADMIN_TOKEN` is the admin token injected via environment variables

---

## 12.4 Expected Result

On success, the response will resemble:

```json
{
  "status": "created",
  "damaged_product_id": "7222956392581",
  "variants": [
    {
      "condition": "Light Damage",
      "variant_id": "41455508619397",
      "quantity_set": 12
    },
    {
      "condition": "Moderate Damage",
      "variant_id": "41455508652165",
      "quantity_set": 0
    },
    {
      "condition": "Heavy Damage",
      "variant_id": "41455508684933",
      "quantity_set": 0
    }
  ],
  "messages": ["Damaged product created successfully."]
}
```

After this:
- The damaged product exists in Shopify as a **draft or published product** (depending on inventory rules)
- Inventory webhooks fire automatically
- Supabase inventory rows are created
- Canonical metafields and redirects are applied

---

## 12.5 Important Warnings

- **Never** manually create damaged products in Shopify
- **Never** edit damaged product variants directly
- **Never** use this endpoint outside controlled testing
- This endpoint bypasses all UI safeguards and is intentionally strict

All operational usage must go through approved tooling.

---

# License
Internal / Proprietary