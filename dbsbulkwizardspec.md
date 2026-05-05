# Damaged Books Service — Bulk Create Wizard
## Technical Specification & Bug Tracker
*Last updated: 2025-05*

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [Data Model & Condition Rules](#2-data-model--condition-rules)
3. [Bug Registry](#3-bug-registry)
4. [Wizard UX — Current vs. Target](#4-wizard-ux--current-vs-target)
5. [Camera Scanning Flow — Redesign](#5-camera-scanning-flow--redesign)
6. [Backend Changes Required](#6-backend-changes-required)
7. [Implementation Plan — Ordered Work](#7-implementation-plan--ordered-work)
8. [File Map](#8-file-map)
9. [API Reference](#9-api-reference)
10. [Shopify IDs & Constants](#10-shopify-ids--constants)

---

## 1. System Architecture

```
Admin Dashboard (React/Vite)
  └─ DamagedBooksWizard.tsx        ← main wizard component
  └─ ScannerModal.tsx              ← camera barcode scanner
  └─ DamagedBooksService.ts        ← API client (fetch wrapper)
  └─ ConfirmModal.tsx              ← generic confirm dialog

Backend (FastAPI / Python)
  └─ backend/app/main.py           ← app entrypoint
  └─ backend/app/admin_routes.py   ← /admin/* endpoints
  └─ backend/app/routes.py         ← /api/* + webhooks
  └─ backend/app/schemas.py        ← Pydantic models
  └─ services/product_service.py   ← Shopify product creation logic
  └─ services/shopify_client.py    ← REST + GraphQL Shopify client
  └─ services/inventory_service.py ← inventory resolution + stock checks
  └─ services/supabase_client.py   ← Supabase client
  └─ services/creation_log_service.py

Database (Supabase / Postgres)
  └─ schema: damaged
     └─ damaged.inventory          ← per-variant inventory mirror
     └─ damaged.inventory_view     ← view with stock_status
     └─ damaged.creation_log       ← audit log of all wizard runs
     └─ damaged.reconcile_log      ← cron reconcile history
     └─ damaged.changelog          ← field-level change audit

Shopify (store)
  └─ Canonical products            ← single-variant new books
  └─ Damaged products              ← 3-variant (light/moderate/heavy)
  └─ Collection: damaged-books     ← id: 279535911045  (manual collection)
  └─ Taxonomy category: Print Books ← gid://shopify/TaxonomyCategory/me-1-3
                                       Media > Books > Print Books
                                       Set via productCategory field (NOT a collection)
```

### Request flow: Bulk Create Wizard

```
User fills in book rows (ISBN + 3 qty fields each)
  │
  ▼
POST /admin/bulk-create/preview
  │  resolve_bulk_inputs() — ISBN → product via GraphQL
  │  compute_damaged_variant_preview() — pure, no writes
  ▼
Frontend shows preview table (confirm modal)
  │
  ▼
POST /admin/bulk-create  (BulkCreateConfirmRequest)
  │  create_damaged_from_preview_items()
  │    ├─ groups items by canonical_product_id
  │    ├─ checks if damaged product already exists
  │    │    ├─ EXISTS → _update_existing_damaged_inventory()
  │    │    └─ NEW   → create_damaged_product_with_duplicate_check()
  │    │                  → create_damaged_pair()  [Shopify REST]
  │    │                  → _apply_initial_inventory()
  │    │                  → publishablePublish()           [Shopify GQL]  ← PENDING
  │    │                  → _add_to_damaged_collection()   [Shopify GQL]  ← DONE
  │    │                  → _set_product_category()        [Shopify GQL]  ← PENDING
  │    │                              (productUpdate → category: TaxonomyCategory/me-1-3)
  │    └─ log_creation_event()  [Supabase]
  ▼
Response → wizard shows success/error per book
```

---

## 2. Data Model & Condition Rules

### Damage conditions

| Key        | Display Name     | Default Discount |
|------------|------------------|-----------------|
| `light`    | Light Damage     | 15% off         |
| `moderate` | Moderate Damage  | 30% off         |
| `heavy`    | Heavy Damage     | 60% off         |

### Handle convention

```
Canonical:  {slug}                         e.g. salt-fat-acid-heat
Damaged:    {slug}-damaged                 e.g. salt-fat-acid-heat-damaged
```

### Variant option

All damaged products use a single Shopify option named **`Condition`** with values:
`Light Damage`, `Moderate Damage`, `Heavy Damage`.

### Barcode convention (synthetic)

```
{snake_case_handle}_{condition_key}
e.g. salt_fat_acid_heat_light
```

### Field inheritance from canonical

| Field             | Inherited? | Notes                                      |
|-------------------|------------|--------------------------------------------|
| Title             | Derived    | First clause + ": Damaged"                 |
| Handle            | Derived    | canonical-handle + "-damaged"              |
| Vendor            | Yes        |                                            |
| Product type      | Yes        |                                            |
| Tags              | Yes + add  | Canonical tags + `["damaged"]`             |
| Cover image       | Yes        | First image only                           |
| Weight            | Yes ✓      | Fixed in v2 — all 3 variants inherit       |
| Weight unit       | Yes ✓      | Fixed in v2                                |
| SKU               | Yes        | Same SKU (= author) across all conditions  |
| Price             | Computed   | canonical_price × (1 − discount_pct)       |
| Body HTML         | Static     | DBS boilerplate copy (fixed, do not edit)  |
| Status at create  | `draft`    | Published in same request — see §6.1       |
| Taxonomy category | Set        | Media > Books > Print Books (me-1-3) via productUpdate — see §6.2 |
| Collection        | Added      | damaged-books collection (279535911045) via collectionAddProducts |

---

## 3. Bug Registry

Status legend: ✅ Fixed | 🔴 Open | 🟡 In Progress | ⚪ Not Started

---

### 3.1 ✅ FIXED — Quantity update path missing for existing damaged products

**File:** `services/product_service.py`  
**Symptom:** Running the wizard for a book that already had a damaged product silently aborted with "Duplicate or conflict detected." Quantities were never updated.  
**Root cause (3 sub-bugs):**

- (A) `admin_routes.py` checked `hasattr(item, "damaged_handle")` — that field does not exist on `BulkCreateConfirmItem`. Every confirm call 422'd.
- (B) `create_damaged_from_preview_items` opened with `isinstance(item, dict)` but items are Pydantic model instances. Always raised.
- (C) `BulkCreateConfirmItem.inventory` is a single `int` (quantity for one condition), but the function expected `{"light": n, "moderate": n, "heavy": n}`.

**Fix:**
- Rewrote `create_damaged_from_preview_items` to group items by `canonical_product_id`, reconstruct the full inventory dict, then route: **existing → `_update_existing_damaged_inventory`** / **new → create path**.
- Fixed admin_routes.py validation to check only `canonical_handle` and `condition_key`.

---

### 3.2 ✅ FIXED — Weight not inherited from canonical product

**File:** `services/product_service.py` → `create_damaged_pair()`  
**Symptom:** All damaged variants had no weight, causing Shopify shipping rate miscalculation.  
**Root cause:** `weight` and `weight_unit` were never read from `canonical_variant` and never included in the variant payload dict.  
**Fix:** Added weight extraction from `canonical_variant` and conditional injection into each variant payload.

---

### 3.3 ✅ FIXED — Damaged products not added to damaged-books collection

**File:** `services/product_service.py`  
**Symptom:** Newly created damaged products did not appear in the damaged-books collection.  
**Root cause:** No `collectionAddProducts` call existed anywhere in the codebase.  
**Fix:** Added `_add_to_damaged_collection()` using the `collectionAddProducts` GraphQL mutation. Called after `_apply_initial_inventory` on fresh creates and after qty-set on updates, only when ≥1 variant has stock.

---

### 3.4 ✅ FIXED — check_damaged_duplicate TypeError on /check-duplicate endpoint

**File:** `backend/app/admin_routes.py` + `services/product_service.py`  
**Symptom:** `POST /admin/check-duplicate` raised a TypeError because the endpoint called `check_damaged_duplicate(canonical_handle=...)` but the function signature required both `canonical_handle` and `damaged_handle`.  
**Fix:** Made `damaged_handle` optional; auto-derives as `canonical_handle + "-damaged"` when not supplied.

---

### 3.5 ✅ FIXED — Camera scanner fires continuously with no user control

**File:** `src/damaged/ScannerModal.tsx`  
**Symptom:** Opening the scanner immediately started a continuous BarcodeDetector polling loop. The ISBN field filled on its own with no user action. No way to verify or reject a detected value.  
**Fix:** Full rewrite. New flow:
1. Live camera — no scanning running
2. User presses circular **Capture** shutter button
3. Frame is frozen; `BarcodeDetector` runs on that single frame
4. If code found → green card shows the value with **"Use This Code" / "Retry"** actions
5. If not found → yellow card prompts retry
6. Accept → `onScan()` fires, modal closes

---

### 3.6 ✅ FIXED — Damaged products not published after creation

**File:** `services/product_service.py`  
**Symptom:** All wizard-created products sat in `status: draft` indefinitely.  
**Fix:** Added `_publish_product(product_id)` — two-step approach:
1. `productUpdate` → `status: ACTIVE` (Online Store channel)
2. `publishablePublish` with all publication IDs fetched via `_get_publication_ids()` (cached at module level, queried once per process lifetime — covers POS and any other connected channels)

Called unconditionally on fresh creates (Step 7c). Called on the update path only if `current_status != "active"` to avoid re-publishing intentionally-offline products.

---

### 3.7 ✅ FIXED — Damaged products not assigned Print Books taxonomy category

**File:** `services/product_service.py`  
**Symptom:** Damaged products were not assigned the Shopify Standard Product Taxonomy category "Media > Books > Print Books".  
**Important:** This is NOT a collection — it is set via `productUpdate.category`, not `collectionAddProducts`.  
**Fix:** Added `_set_product_category(product_id)` which calls `productUpdate` with `category: "gid://shopify/TaxonomyCategory/me-1-3"`. Idempotent. Called on both fresh creates (Step 7d) and the update-existing path (when any variant has stock).

---

### 3.8 🔴 OPEN — Wizard UX: one bulk textarea is illogical

**File:** `src/DamagedBooksWizard.tsx`  
**Symptom:** The wizard shows a single large textarea where all ISBNs are pasted/scanned, plus three global qty fields (light/moderate/heavy). The same qty numbers apply to every book in the batch. This is wrong — each book needs its own qty inputs.  
**Fix required:** Full UX redesign — see §4.

---

### 3.9 🔴 OPEN — Camera scanning flow has no per-book qty step

**File:** `src/damaged/ScannerModal.tsx`  
**Symptom:** After scanning, the code is dropped into the global textarea with no prompt to enter quantities for that specific book. The scanner and the qty fields are disconnected.  
**Fix required:** Scanner modal needs a qty-entry step after a successful scan. See §5.

---

## 4. Wizard UX — Current vs. Target

### 4.1 Current (broken) layout

```
┌─────────────────────────────────────────────┐
│ Source Products                             │
│ [📷 Scan via Camera]                        │
│ ┌─────────────────────────────────────────┐ │
│ │ 9780385340533, 9780062316110, ...       │ │  ← one big textarea
│ └─────────────────────────────────────────┘ │
│                                             │
│ Light Qty  Moderate Qty  Heavy Qty          │  ← same qty for ALL books
│ [  3    ]  [    2      ] [   1   ]          │
│                                             │
│                        [Generate Preview]   │
└─────────────────────────────────────────────┘
```

**Problem:** 3 copies light damage of every book in the list. No individual control.

---

### 4.2 Target layout — per-book rows

```
┌─────────────────────────────────────────────────────────────────┐
│ Bulk Create Damaged Books                                       │
│ Step 1: Add books and set quantities per condition              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Book 1                                                    [✕] │
│  ISBN / Product ID  ──────────────────────  [📷 Scan]          │
│  [  9780385340533                        ]                      │
│                                                                 │
│  Light Damage    Moderate Damage    Heavy Damage                │
│  [    2        ] [       1        ] [     0    ]                │
│                                                                 │
├─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┤
│                                                                 │
│  Book 2                                                    [✕] │
│  ISBN / Product ID  ──────────────────────  [📷 Scan]          │
│  [  9780062316110                        ]                      │
│                                                                 │
│  Light Damage    Moderate Damage    Heavy Damage                │
│  [    0        ] [       3        ] [     1    ]                │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  [+ Add Another Book]                                           │
│                                                                 │
│                                         [Generate Preview →]   │
└─────────────────────────────────────────────────────────────────┘
```

### 4.3 Data model for wizard rows (frontend state)

```typescript
type BookRow = {
  id: string;           // uuid — React key only
  isbn: string;         // raw user input
  inventory: {
    light: number;
    moderate: number;
    heavy: number;
  };
};
```

State:
```typescript
const [rows, setRows] = useState<BookRow[]>([emptyRow()]);
```

### 4.4 Row operations

| Action             | Behaviour                                                      |
|--------------------|----------------------------------------------------------------|
| Add another book   | Appends a new empty `BookRow` at the bottom                   |
| Remove row [✕]     | Removes that row; minimum 1 row always present                 |
| Scan via Camera    | Opens `ScannerModal` with that row's index as context          |
| On scan accept     | ISBN drops into that row's ISBN field; modal closes            |
| Generate Preview   | Validates all rows have non-empty ISBN, then POST preview      |

### 4.5 Validation before preview

- Every row must have a non-empty ISBN/product ID
- At least one qty > 0 across all rows (warn if submitting all-zero)
- Duplicate ISBNs within the batch → warn, do not block

### 4.6 Preview table (confirm modal)

Grouped by book, then condition:

```
┌──────────────────────────────────────────────────────┐
│ Title                  Condition    Qty   Discount   │
├──────────────────────────────────────────────────────┤
│ Salt Fat Acid Heat     Light        2     −15%       │
│ Salt Fat Acid Heat     Moderate     1     −30%       │
│ Sapiens                Moderate     3     −30%       │
│ Sapiens                Heavy        1     −60%       │
└──────────────────────────────────────────────────────┘
```

Zero-qty rows are **excluded** from the preview (and from the confirm payload).

---

## 5. Camera Scanning Flow — Redesign

### 5.1 Current flow (post scanner fix)

```
Tap [📷 Scan] → live camera → tap shutter → detected / retry → accept → ISBN into textarea
```

### 5.2 Target flow — per-book, with qty step

```
Tap [📷 Scan] on a specific row
  │
  ▼
ScannerModal opens
  │
  ▼
Live camera → tap shutter → BarcodeDetector on frozen frame
  │
  ├─ Not detected → retry prompt (same as current)
  │
  └─ Detected → show code in green card
                 + show 3 qty inputs (light / moderate / heavy) inline in modal
                 + [Add Book] button
                 + [Retry] button
                 │
                 ├─ [Add Book] → calls onScan(isbn, inventory)
                 │               wizard appends/updates that row
                 │               modal stays open (camera resumes)
                 │               shows "Added! Scan next book or close."
                 │
                 └─ [Retry] → unfreezes camera, clears qty inputs, back to scanning
```

### 5.3 ScannerModal prop interface update

```typescript
// Current
type ScannerModalProps = {
  isOpen: boolean;
  onClose: () => void;
  onScan: (value: string) => void;
};

// Target
type ScannerModalProps = {
  isOpen: boolean;
  onClose: () => void;
  onScan: (isbn: string, inventory: { light: number; moderate: number; heavy: number }) => void;
};
```

### 5.4 Wizard handler update

```typescript
// DamagedBooksWizard.tsx
const handleScanResult = (
  isbn: string,
  inventory: { light: number; moderate: number; heavy: number }
) => {
  // Find the target row (the one that opened the scanner)
  setRows(prev => prev.map((row, i) =>
    i === activeScanRowIndex
      ? { ...row, isbn, inventory }
      : row
  ));
};
```

---

## 6. Backend Changes Required

### 6.1 🔴 Publish to all sales channels after creation

**Where:** `services/product_service.py` → `create_damaged_product_with_duplicate_check()`, after Step 7b (collection add).

**Approach:** Use the Shopify Admin GraphQL `publishablePublish` mutation. This publishes to all connected publication channels in one call.

```graphql
mutation PublishProduct($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    publishable {
      availablePublicationCount
      publicationCount
    }
    userErrors { field message }
  }
}
```

To publish to ALL channels, first query `publications` to get all publication IDs, then pass them all. Alternatively, use `productUpdate` to set `status: "ACTIVE"` which publishes to the Online Store channel:

```graphql
mutation ActivateProduct($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id status }
    userErrors { field message }
  }
}
# variables: { "input": { "id": "gid://shopify/Product/...", "status": "ACTIVE" } }
```

**Recommended approach:** 
1. `productUpdate` → set `status: ACTIVE` (publishes Online Store).
2. Separately call `publishablePublish` with all known publication GIDs for other channels (POS, etc.).

**New function to add:**
```python
async def _publish_product(product_id: str) -> None:
    """Publish a newly created damaged product to all sales channels."""
    ...
```

**Also applies to `_update_existing_damaged_inventory`** — if an existing product was in draft and now has stock, it should be published.

---

### 6.2 🔴 Set Print Books taxonomy category via productUpdate

**Where:** `services/product_service.py`  
**Important distinction:** This is the **Shopify Standard Product Taxonomy** — not a collection. It is set on the product record itself via the `productCategory` / `category` field, not via `collectionAddProducts`. You cannot add a product to a taxonomy node; you assign the taxonomy node to the product.

**Taxonomy details:**
| Field | Value |
|-------|-------|
| Display name | Print Books |
| Breadcrumb | Media > Books > Print Books |
| Category GID | `gid://shopify/TaxonomyCategory/me-1-3` |

**GraphQL mutation:**
```graphql
mutation SetProductCategory($input: ProductInput!) {
  productUpdate(input: $input) {
    product {
      id
      category {
        id
        name
        fullName
      }
    }
    userErrors { field message }
  }
}
```

**Variables:**
```json
{
  "input": {
    "id": "gid://shopify/Product/987654321",
    "category": "gid://shopify/TaxonomyCategory/me-1-3"
  }
}
```

**New function to add:**
```python
PRINT_BOOKS_TAXONOMY_GID = "gid://shopify/TaxonomyCategory/me-1-3"

async def _set_product_category(product_id: str) -> None:
    """
    Assign the Shopify Standard Product Taxonomy category 'Print Books'
    (Media > Books > Print Books) to a product.

    This is NOT a collection — it sets the productCategory field on the product
    itself via productUpdate. It applies to all channels and Shopify taxonomy
    reporting.
    """
    raw_id = str(product_id).split("/")[-1]
    product_gid = f"gid://shopify/Product/{raw_id}"

    mutation = """
    mutation SetProductCategory($input: ProductInput!) {
      productUpdate(input: $input) {
        product {
          id
          category { id name }
        }
        userErrors { field message }
      }
    }
    """
    variables = {
        "input": {
            "id": product_gid,
            "category": PRINT_BOOKS_TAXONOMY_GID,
        }
    }

    try:
        resp = await shopify_client.graphql(mutation, variables)
        body = resp.get("body", resp)
        errors = (body.get("data") or {}).get("productUpdate", {}).get("userErrors") or []
        if errors:
            logger.warning("[Taxonomy] Errors setting category on %s: %s", product_gid, errors)
        else:
            logger.info("[Taxonomy] Set Print Books category on product %s", product_gid)
    except Exception as e:
        logger.warning("[Taxonomy] Failed to set category on %s: %s", product_gid, e)
```

**Wire-up points** (same two places as collection add):
1. `create_damaged_product_with_duplicate_check()` — after Step 7b (`_add_to_damaged_collection`)
2. `_update_existing_damaged_inventory()` — after collection add (only if previously uncategorised, but safe to call every time — idempotent)

---

### 6.3 Preview endpoint changes for per-book rows

The preview endpoint already accepts `inputs[]` (one input per ISBN) + a single `inventory` seed.  
With the new UX, **each input carries its own inventory**. The schema needs updating.

**Current schema:**
```python
class BulkCreateRequest(BaseModel):
    inputs: Optional[List[BulkCreateInput]] = None   # [{type, value}]
    inventory: Optional[InventorySeed] = None         # ONE global {light, moderate, heavy}
```

**Target schema:**
```python
class BulkCreateInput(BaseModel):
    type: str           # "isbn" | "product_id"
    value: str
    inventory: InventorySeed  # per-input quantities  ← NEW FIELD

class BulkCreateRequest(BaseModel):
    inputs: Optional[List[BulkCreateInput]] = None
    # inventory (global) removed or kept for legacy compat
```

**Backend preview function update:**
`compute_damaged_variant_preview()` will receive the input's own `inventory` instead of a global seed.

---

### 6.4 BulkCreateConfirmItem — no schema changes needed

`BulkCreateConfirmItem` is already per-condition-per-book (flat list). The grouping logic added in the fix handles this correctly. No schema changes needed for the confirm step.

---

## 7. Implementation Plan — Ordered Work

Work is ordered by dependency and risk. Complete each phase before starting the next.

---

### Phase 1 — Already Deployed (v2 fixes)

- ✅ 3.1 Qty update for existing damaged products
- ✅ 3.2 Weight inheritance
- ✅ 3.3 Damaged-books collection membership
- ✅ 3.4 check_damaged_duplicate TypeError
- ✅ 3.5 Scanner shutter-button flow

**Files changed:** `product_service.py`, `admin_routes.py`, `ScannerModal.tsx`

---

### Phase 2 — ✅ Complete — Backend: Publish + Taxonomy Category

- ✅ 3.6 Publish to all sales channels (`_publish_product`)
- ✅ 3.7 Set Print Books taxonomy category (`_set_product_category`)

**Files changed:** `product_service.py`

---

### Phase 3 — ✅ Complete — Frontend: Per-Book Row Wizard

- ✅ 3.8 Per-book row UX — `BookRow` type + `BookRowInput` sub-component
- ✅ Replaced global textarea + shared qty fields with individual book rows
- ✅ Each row: ISBN field + per-condition qty inputs (Light/Moderate/Heavy) + Scan button + Remove button
- ✅ "Add Another Book" button
- ✅ Preview calls parallel via `Promise.allSettled` — one call per book row, each with its own inventory seed (no backend schema change needed)
- ✅ Zero-qty conditions filtered before preview table and confirm payload
- ✅ Preview table grouped by book with group header rows
- ✅ Partial failure warnings (some books resolve, others don't)
- ✅ Scanner wired per-row via `activeScanRowIndex` state
- ✅ Confirming spinner overlay (backdrop-blur)
- ✅ Success message updated to reflect publish + collection

**Files changed:** `DamagedBooksWizard.tsx`  
**No backend changes** — preview endpoint called once per row with per-row inventory seed.

---

### Phase 4 — ✅ Complete — Frontend: Scanner Per-Book Qty Flow

- ✅ `ScannerModal` fully rewritten with `'added'` state added to state machine
- ✅ `detected` state now shows colour-coded qty inputs (Light/Moderate/Heavy) before confirming
- ✅ "Add Book" button replaces "Use This Code" — fires `onScan(isbn, inventory)`, shows `added` flash
- ✅ `added` state: green overlay on camera + confirmation card with ISBN + qty summary + auto-resume spinner
- ✅ Camera auto-resumes after 1.8s — modal stays open for next scan, no manual re-open needed
- ✅ `onScan` prop signature updated: `(isbn: string, inventory: InventorySeed) => void`
- ✅ Wizard `handleScan` updated: fills target row with both ISBN and inventory, appends new empty row, advances `activeScanRowIndex` — scanner stays open
- ✅ Each "Add Book" press in scanner pre-adds a new wizard row and advances the index so the next scan lands in the right place

**Files changed:** `ScannerModal.tsx`, `DamagedBooksWizard.tsx`

---

### Phase 5 — ✅ Complete — Polish & Edge Cases

- ✅ **Duplicate ISBN detection** — `handlePreview` builds an isbn→count map before firing API calls; duplicates trigger a yellow warning and are de-duplicated (first occurrence kept). Done before `setBusy(true)` so it's instant, no API cost.
- ✅ **Zero-qty filter** — already in place from Phase 3; zero-qty conditions never reach preview or confirm.
- ✅ **Per-book result breakdown** — `ConfirmResponse` type expanded to `{ results: BookResult[], errors: BookError[], meta }`. `handleConfirm` now always advances to the `result` phase (even on partial failure) instead of falling back to a generic error.
- ✅ **Result modal redesign** — summary pills (N created / N updated / N failed) + scrollable per-book list with status icons (✓ green / ↻ blue / ✗ red), status badge, first message, and external link to Shopify Admin.
- ✅ **Shopify Admin links** — `shopifyAdminProductUrl()` helper builds `https://admin.shopify.com/store/${VITE_SHOPIFY_STORE_HANDLE}/products/${id}`. Returns `null` (no link rendered) if `VITE_SHOPIFY_STORE_HANDLE` isn't set.
- ✅ **Fallback** — if backend returns neither `results` nor `errors` (legacy shape), falls back to `result.message` / `result.error` text.

**Files changed:** `DamagedBooksWizard.tsx`  
**New env var needed:** `VITE_SHOPIFY_STORE_HANDLE` — the Shopify store handle (e.g. `kitchenartsandletters`). Optional; links are omitted if not set.

---

## 8. File Map

| File | Role | Last changed |
|------|------|--------------|
| `src/DamagedBooksWizard.tsx` | Main wizard component | v1 — needs Phase 3 rewrite |
| `src/damaged/ScannerModal.tsx` | Camera scanner modal | v2 ✅ — needs Phase 4 qty step |
| `src/DamagedBooksService.ts` | API client | unchanged |
| `src/ConfirmModal.tsx` | Generic confirm dialog | unchanged |
| `backend/app/admin_routes.py` | /admin/* endpoints | v2 ✅ |
| `backend/app/schemas.py` | Pydantic models | needs Phase 2 update (BulkCreateInput) |
| `services/product_service.py` | Creation + inventory logic | v2 ✅ — needs Phase 2 additions |
| `services/shopify_client.py` | HTTP client | unchanged |
| `services/inventory_service.py` | Webhook + stock resolution | unchanged |
| `services/creation_log_service.py` | Supabase audit log | unchanged |
| `db/migrations/0001_init_damaged_inventory.sql` | DB schema | unchanged |

---

## 9. API Reference

### POST /admin/bulk-create/preview

**Auth:** `X-Admin-Token` header  
**Body:**
```json
{
  "inputs": [
    { "type": "isbn", "value": "9780385340533" }
  ],
  "inventory": { "light": 2, "moderate": 1, "heavy": 0 }
}
```
**Response:**
```json
{
  "ok": true,
  "preview": [
    {
      "canonical_product_id": "123456789",
      "canonical_handle": "salt-fat-acid-heat",
      "condition": "light",
      "title": "Light Damage",
      "price": "29.75",
      "discount_pct": 0.15,
      "inventory_seed": 2,
      "sku": "Author Name",
      "barcode": "salt_fat_acid_heat_light"
    }
  ],
  "meta": { "count": 3 }
}
```

### POST /admin/bulk-create (confirm)

**Auth:** `X-Admin-Token` header  
**Body:**
```json
{
  "items": [
    {
      "canonical_product_id": 123456789,
      "canonical_handle": "salt-fat-acid-heat",
      "condition_key": "light",
      "inventory": 2
    },
    {
      "canonical_product_id": 123456789,
      "canonical_handle": "salt-fat-acid-heat",
      "condition_key": "moderate",
      "inventory": 1
    }
  ]
}
```
**Response:**
```json
{
  "ok": true,
  "results": [
    {
      "status": "created",
      "damaged_product_id": "987654321",
      "damaged_handle": "salt-fat-acid-heat-damaged",
      "variants": [...],
      "messages": ["Damaged product created successfully."]
    }
  ],
  "errors": [],
  "meta": { "processed": 1, "succeeded": 1, "failed": 0 }
}
```

**Note:** `status` in each result will be `"created"` (new product) or `"updated"` (existing product, qty refreshed).

---

## 10. Shopify IDs & Constants

| Name | Value | Type |
|------|-------|------|
| Damaged Books collection | `279535911045` | Manual collection — `collectionAddProducts` |
| Damaged Books collection GID | `gid://shopify/Collection/279535911045` | — |
| Print Books taxonomy | `gid://shopify/TaxonomyCategory/me-1-3` | Taxonomy category — `productUpdate.category` |
| Print Books breadcrumb | `Media > Books > Print Books` | — |
| Default location | set via `DBS_SHOPIFY_LOCATION_ID` env var | — |

### Key distinction: Collection vs. Taxonomy Category

| | Collection | Taxonomy Category |
|---|---|---|
| Example | damaged-books | Print Books |
| How set | `collectionAddProducts` mutation | `productUpdate` with `category` field |
| Stored on | Collection membership record | Product record itself |
| Multiple per product | Yes | One at a time |
| Shopify Admin path | Products → Collections | Product → Category field |

### Environment variables

| Var | Purpose |
|-----|---------|
| `DBS_SHOPIFY_LOCATION_ID` | Primary location for inventory writes (preferred) |
| `SHOPIFY_LOCATION_ID` | Fallback location |
| `SHOPIFY_ACCESS_TOKEN` | Admin API token |
| `VITE_DBS_ADMIN_TOKEN` | Admin dashboard shared secret |
| `GATEWAY_LOGS_URL` | Optional: link to log viewer |