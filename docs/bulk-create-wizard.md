Bulk Create Wizard — Technical Specification

Damaged Books Service (DBS) ↔ Admin Dashboard (AD)

⸻

Overview

The Bulk Create Wizard is the primary creation interface for damaged book listings in Shopify.

It is implemented as:
	•	A modal-driven workflow in the KAL Admin Dashboard (AD)
	•	Backed by explicit, stateful endpoints in the Damaged Books Service (DBS)

This document defines:
	•	Input modes and normalization rules
	•	Request / response shapes
	•	Validation and duplicate-prevention semantics
	•	Preview vs write phases
	•	Error contracts
	•	Inventory seeding behavior

This spec is authoritative for both UI and backend.

⸻

Architectural Model

Admin Dashboard (React)
   |
   |  (validated, normalized payload)
   v
Damaged Books Service (DBS)
   |
   |  Shopify Admin API
   v
Shopify Product / Variant / Inventory

Key principles:
	•	AD never writes to Shopify directly
	•	DBS is the source of truth for duplication safety
	•	All writes are explicit, logged, and confirm-gated

⸻

Canonical Assumptions
	•	Every damaged product is derived from exactly one canonical product
	•	Canonical products must be:
        •	Single-variant
        •	Already present in Shopify
	•	DBS enforces:
        •	One damaged product per canonical product
        •	Stable handle and variant structure
        •	Discount logic per condition

⸻

Wizard Phases

The wizard operates in two explicit phases:
	1.	Preview / Dry Run
	2.	Confirm + Write

No write operations occur during preview.

⸻

Supported Input Modes

Input Mode 1: ISBN

Purpose: Primary staff workflow

Normalization
	•	Split on whitespace or commas
	•	Trim
	•	Deduplicate

Resolution
	•	ISBN → Shopify variant.barcode → canonical product.id

⸻

Input Mode 2: Product ID (Advanced)

Purpose
	•	Engineering recovery
	•	Audit / reconciliation
	•	Fallback when ISBN is missing or malformed

Rules
	•	Must be numeric Shopify Product ID
	•	Must resolve to a single-variant product

⸻

Input Mode 3: CSV Upload

Required Headers

isbn

or

product_id

Optional Headers

quantity_light
quantity_moderate
quantity_heavy

Notes
	•	Quantities default to 0 if omitted
	•	Each row maps to one canonical product
	•	CSV parsing happens client-side first, then server-validated

⸻

API: Preview Endpoint

Endpoint

POST /api/damaged/bulk-create/preview

Purpose
	•	Resolve inputs
	•	Enforce eligibility
	•	Detect duplicates
	•	Return exactly what would be created

Request Body

{
  "inputs": [
    {
      "type": "isbn",
      "value": "9781324073796"
    },
    {
      "type": "product_id",
      "value": "1234567890"
    }
  ],
  "inventory": {
    "light": 2,
    "moderate": 1,
    "heavy": 0
  }
}

Server Responsibilities

For each input:
	1.	Resolve canonical product
	2.	Validate:
        •	Exists
        •	Single-variant
        •	Not already damaged
	3.	Compute:
        •	Damaged handle
        •	Title
        •	Variant structure
        •	Discount per condition
	4.	Detect conflicts:
        •	Existing damaged product
        •	Supabase creation_log entries

⸻

Preview Response (Success)

{
  "ok": true,
  "preview": [
    {
      "canonical": {
        "product_id": 1234567890,
        "title": "The Irish Bakery",
        "handle": "the-irish-bakery"
      },
      "damaged_product": {
        "handle": "the-irish-bakery-damaged",
        "title": "The Irish Bakery (Damaged)",
        "variants": [
          {
            "condition": "light",
            "price_modifier": -15,
            "inventory": 2
          },
          {
            "condition": "moderate",
            "price_modifier": -30,
            "inventory": 1
          },
          {
            "condition": "heavy",
            "price_modifier": -60,
            "inventory": 0
          }
        ]
      }
    }
  ]
}

This response is rendered directly into the ConfirmModal preview UI.

⸻

Preview Response (Error)

{
  "ok": false,
  "errors": [
    {
      "input": "9781324073796",
      "reason": "ALREADY_HAS_DAMAGED_PRODUCT"
    }
  ]
}

Errors are actionable, not silent.

⸻

API: Confirm + Create Endpoint

Endpoint

POST /api/damaged/bulk-create/confirm

Purpose
	•	Perform irreversible writes
	•	Create damaged product(s)
	•	Initialize inventory
	•	Log creation

Request Body

{
  "confirmed": true,
  "items": [
    {
      "canonical_product_id": 1234567890,
      "inventory": {
        "light": 2,
        "moderate": 1,
        "heavy": 0
      }
    }
  ]
}

This payload must match the preview response.
Any drift → request rejected.

⸻

Server Write Sequence (Per Item)
	1.	Re-validate canonical product
	2.	Re-check duplication safety
	3.	Create damaged product in Shopify
	4.	Create condition variants
	5.	Apply discount pricing
	6.	Initialize inventory per variant
	7.	Write to creation_log
	8.	Evaluate collection publication rules

⸻

Confirm Response (Success)

{
  "ok": true,
  "created": [
    {
      "canonical_product_id": 1234567890,
      "damaged_product_id": 9988776655
    }
  ]
}


⸻

Confirm Response (Partial Failure)

{
  "ok": false,
  "created": [],
  "errors": [
    {
      "canonical_product_id": 1234567890,
      "reason": "HANDLE_COLLISION"
    }
  ]
}

No silent partial writes are allowed.

⸻

Inventory Semantics
	•	Inventory is only seeded at creation time via this wizard
	•	Subsequent inventory changes:
	•	Allowed via Shopify Admin
	•	Tracked via inventory webhooks
	•	DBS reacts to inventory changes but does not initiate them post-creation

⸻

Duplication Safety (Critical)

Shopify does not enforce conceptual uniqueness.

DBS enforces:
	•	One damaged product per canonical product
	•	Stable handle derivation
	•	Creation-time locking via Supabase

The Bulk Create Wizard is the UI expression of these protections.

⸻

Modal UX Contract (AD)

The modal must:
	•	Render preview data exactly as returned
	•	Require explicit confirmation
	•	Disable confirm while request is in flight
	•	Surface errors verbatim
	•	Never “optimistically assume” success

ConfirmModal.tsx is the canonical container.

⸻

Logging & Audit

Each confirmed create writes:
	•	Canonical product ID
	•	Damaged product ID
	•	Timestamp
	•	Operator (if available)
	•	Inventory snapshot

Stored in:

supabase.creation_log


⸻

Summary

This wizard:
	•	Is not a convenience feature
	•	Is a safety-critical system boundary
	•	Encodes business rules Shopify cannot

AD implements UI.
DBS enforces reality.

Damaged Books Service — Bulk Create Wizard API Contract

This section defines the exact routes, payloads, and response shapes the Admin Dashboard (AD) must integrate against.

These endpoints are admin-only, require authentication, and are stateful.

⸻

Authentication (All Routes)

Header (required):

X-Admin-Token: <VITE_DBS_ADMIN_TOKEN>

Requests without this header MUST return:

401 Unauthorized


⸻

1️⃣ Preview / Dry-Run Endpoint

Route

POST /api/damaged/bulk-create/preview

Purpose
	•	Resolve canonical products
	•	Validate eligibility
	•	Detect duplicates or conflicts
	•	Compute exactly what would be created
	•	Perform zero writes

This endpoint is safe to call repeatedly.

⸻

Request Body

{
  "inputs": [
    {
      "type": "isbn",
      "value": "9781324073796"
    },
    {
      "type": "product_id",
      "value": "1234567890"
    }
  ],
  "inventory": {
    "light": 2,
    "moderate": 1,
    "heavy": 0
  }
}

Input Rules
	•	inputs[]
	•	type: "isbn" | "product_id"
	•	value: string (validated server-side)
	•	inventory
	•	Missing keys default to 0
	•	Values must be >= 0

⸻

Preview Response — Success

{
  "ok": true,
  "preview": [
    {
      "canonical": {
        "product_id": 1234567890,
        "title": "The Irish Bakery",
        "handle": "the-irish-bakery"
      },
      "damaged_product": {
        "handle": "the-irish-bakery-damaged",
        "title": "The Irish Bakery (Damaged)",
        "variants": [
          {
            "condition": "light",
            "price_modifier": -15,
            "inventory": 2
          },
          {
            "condition": "moderate",
            "price_modifier": -30,
            "inventory": 1
          },
          {
            "condition": "heavy",
            "price_modifier": -60,
            "inventory": 0
          }
        ]
      }
    }
  ]
}

Notes for AD
	•	This response must be rendered verbatim in the ConfirmModal
	•	AD must not derive or recompute anything
	•	Ordering is authoritative

⸻

Preview Response — Error

{
  "ok": false,
  "errors": [
    {
      "input": "9781324073796",
      "reason": "ALREADY_HAS_DAMAGED_PRODUCT"
    }
  ]
}

Possible reason values (non-exhaustive)
	•	NOT_FOUND
	•	MULTI_VARIANT_CANONICAL
	•	ALREADY_HAS_DAMAGED_PRODUCT
	•	HANDLE_COLLISION
	•	INVALID_INPUT

Notes for AD
	•	Errors are actionable
	•	AD should surface them clearly
	•	Preview modal should not open if ok === false

⸻

2️⃣ Confirm + Create Endpoint

Route

POST /api/damaged/bulk-create/confirm

Purpose
	•	Perform irreversible writes
	•	Create damaged product(s) in Shopify
	•	Initialize inventory
	•	Write creation logs
	•	Apply collection publication rules

This endpoint must never be called optimistically.

⸻

Request Body

{
  "confirmed": true,
  "items": [
    {
      "canonical_product_id": 1234567890,
      "inventory": {
        "light": 2,
        "moderate": 1,
        "heavy": 0
      }
    }
  ]
}

Critical Contract Rule

The items[] payload must exactly match the preview result.

Any drift (missing item, mismatched inventory, reordered conditions) → request rejected.

⸻

Confirm Response — Success

{
  "ok": true,
  "created": [
    {
      "canonical_product_id": 1234567890,
      "damaged_product_id": 9988776655
    }
  ]
}

Notes for AD
	•	Treat success as final
	•	No retry semantics
	•	Clear wizard state after success

⸻

Confirm Response — Failure

{
  "ok": false,
  "created": [],
  "errors": [
    {
      "canonical_product_id": 1234567890,
      "reason": "HANDLE_COLLISION"
    }
  ]
}

Failure Semantics
	•	No silent partial writes
	•	If any item fails, AD must assume nothing succeeded
	•	Errors must be shown verbatim

⸻

3️⃣ Server-Side Guarantees (Important for AD)

DBS guarantees:
	•	Exactly one damaged product per canonical product
	•	Stable handle derivation
	•	Discount logic per condition:
	•	light → −15%
	•	moderate → −30%
	•	heavy → −60%
	•	Inventory only seeded at creation time
	•	All writes logged to supabase.creation_log

AD must assume:
	•	Shopify does not prevent duplication
	•	DBS does

⸻

4️⃣ AD Integration Expectations

AD must:
	•	Use Preview endpoint before Confirm
	•	Block Confirm while request is in flight
	•	Render preview exactly as returned
	•	Never mutate preview data
	•	Never write to Shopify directly

AD must not:
	•	Guess discount logic
	•	Auto-retry confirm
	•	Skip preview phase
	•	Perform partial writes

⸻

5️⃣ Recommended Service Methods (AD)

These are the methods AD should implement:

DamagedBooksService.previewBulkCreate(payload)
DamagedBooksService.confirmBulkCreate(payload)

Where payloads exactly match the JSON above.

⸻

TL;DR for the AD Chat

“Treat DBS like a database, not an API helper.
Preview is read-only truth.
Confirm is irreversible.
If preview and confirm ever diverge, DBS will reject the request.”


⸻

1️⃣ TypeScript Interfaces (AD)

Create (or append to)
src/services/damagedBooks.types.ts

// =============================
// Bulk Create Wizard — Types
// =============================

/**
 * Supported input identifiers
 * Canonical products are ALWAYS single-variant
 */
export type BulkCreateInput =
  | { type: 'isbn'; value: string }
  | { type: 'product_id'; value: string };

/**
 * Inventory quantities per damage condition
 * Missing keys default to 0 server-side
 */
export type DamageInventorySeed = {
  light?: number;
  moderate?: number;
  heavy?: number;
};

/**
 * Preview request payload
 */
export interface BulkCreatePreviewRequest {
  inputs: BulkCreateInput[];
  inventory: DamageInventorySeed;
}

/**
 * Preview variant shape returned by DBS
 */
export interface BulkCreatePreviewVariant {
  condition: 'light' | 'moderate' | 'heavy';
  price_modifier: number; // negative percentage (e.g. -15)
  inventory: number;
}

/**
 * Preview damaged product payload
 */
export interface BulkCreatePreviewDamagedProduct {
  handle: string;
  title: string;
  variants: BulkCreatePreviewVariant[];
}

/**
 * Preview canonical product payload
 */
export interface BulkCreatePreviewCanonical {
  product_id: number;
  title: string;
  handle: string;
}

/**
 * Single preview result row
 */
export interface BulkCreatePreviewItem {
  canonical: BulkCreatePreviewCanonical;
  damaged_product: BulkCreatePreviewDamagedProduct;
}

/**
 * Successful preview response
 */
export interface BulkCreatePreviewSuccess {
  ok: true;
  preview: BulkCreatePreviewItem[];
}

/**
 * Preview error shape
 */
export interface BulkCreatePreviewErrorItem {
  input: string;
  reason:
    | 'NOT_FOUND'
    | 'MULTI_VARIANT_CANONICAL'
    | 'ALREADY_HAS_DAMAGED_PRODUCT'
    | 'HANDLE_COLLISION'
    | 'INVALID_INPUT';
}

/**
 * Failed preview response
 */
export interface BulkCreatePreviewFailure {
  ok: false;
  errors: BulkCreatePreviewErrorItem[];
}

export type BulkCreatePreviewResponse =
  | BulkCreatePreviewSuccess
  | BulkCreatePreviewFailure;

/**
 * Confirm request payload
 * MUST match preview exactly
 */
export interface BulkCreateConfirmItem {
  canonical_product_id: number;
  inventory: {
    light: number;
    moderate: number;
    heavy: number;
  };
}

export interface BulkCreateConfirmRequest {
  confirmed: true;
  items: BulkCreateConfirmItem[];
}

/**
 * Confirm success response
 */
export interface BulkCreateConfirmSuccess {
  ok: true;
  created: {
    canonical_product_id: number;
    damaged_product_id: number;
  }[];
}

/**
 * Confirm failure response
 */
export interface BulkCreateConfirmFailure {
  ok: false;
  created: [];
  errors: {
    canonical_product_id: number;
    reason: string;
  }[];
}

export type BulkCreateConfirmResponse =
  | BulkCreateConfirmSuccess
  | BulkCreateConfirmFailure;


⸻

2️⃣ Fetch Wrappers for DamagedBooksService.ts (AD)

Append the following verbatim to
src/services/DamagedBooksService.ts

This follows the same patterns already used in that file.

⸻

Imports

import {
  BulkCreatePreviewRequest,
  BulkCreatePreviewResponse,
  BulkCreateConfirmRequest,
  BulkCreateConfirmResponse
} from './damagedBooks.types';


⸻

Service Methods

// =============================
// Bulk Create Wizard — API
// =============================

async function post<TReq, TRes>(path: string, body: TReq): Promise<TRes> {
  const res = await fetch(new URL(path, BASE), {
    method: 'POST',
    headers: {
      'X-Admin-Token': ADMIN_API_TOKEN,
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    },
    credentials: 'omit',
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }

  return await res.json() as TRes;
}

export const DamagedBooksService = {
  // … existing methods …

  /**
   * Preview bulk damaged-product creation
   * Zero writes. Safe to retry.
   */
  async previewBulkCreate(
    payload: BulkCreatePreviewRequest
  ): Promise<BulkCreatePreviewResponse> {
    return post<
      BulkCreatePreviewRequest,
      BulkCreatePreviewResponse
    >('/api/damaged/bulk-create/preview', payload);
  },

  /**
   * Confirm and execute bulk damaged-product creation
   * Irreversible. Never retry automatically.
   */
  async confirmBulkCreate(
    payload: BulkCreateConfirmRequest
  ): Promise<BulkCreateConfirmResponse> {
    return post<
      BulkCreateConfirmRequest,
      BulkCreateConfirmResponse
    >('/api/damaged/bulk-create/confirm', payload);
  },
};


⸻

3️⃣ Critical AD Implementation Notes (Do Not Skip)

These are rules, not suggestions — AD should treat them as invariants:

Preview Phase
	•	Must be called before confirm
	•	Errors block wizard progression
	•	Preview data must be rendered verbatim
	•	AD must not infer discount logic

Confirm Phase
	•	Payload must exactly match preview
	•	No optimistic UI
	•	No retries
	•	On success → clear wizard state
	•	On failure → assume no writes occurred

Inventory Semantics
	•	Inventory is seeded once
	•	Post-creation inventory edits are Shopify-native
	•	DBS only reacts via webhooks afterward

⸻

4️⃣ Why This Matters (Context for AD Devs)

This wizard is not a convenience UI.

It exists because:
	•	Shopify has no native duplication guard
	•	Damaged products are structural derivatives, not SKUs
	•	A single mistake creates silent catalog corruption

The wizard + DBS together form a safety boundary.