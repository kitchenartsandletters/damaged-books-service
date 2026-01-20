DBS ↔ AD Contract

Damaged Books Service × Admin Dashboard

This document defines the hard contract between the Damaged Books Service (DBS) and the Admin Dashboard (AD).

If you are working on either side of this boundary, you are expected to follow this contract exactly.
Violations will cause duplicate products, inventory corruption, or irreversible Shopify errors.

⸻

1. System Roles (Non-Negotiable)

Admin Dashboard (AD)

AD is a client-only orchestrator.

AD does not:
	•	Resolve ISBNs or product IDs
	•	Query Shopify directly for damaged-book logic
	•	Decide discounts, variant structure, or inventory rules
	•	Write to Shopify

AD only:
	•	Collects user input
	•	Sends input to DBS
	•	Displays DBS preview output verbatim
	•	Confirms DBS preview for execution

Rule: AD never guesses. AD never recomputes. AD never retries.

⸻

Damaged Books Service (DBS)

DBS is the single source of truth.

DBS owns:
	•	Shopify resolution (ISBN, product_id → canonical product)
	•	Validation (single-variant canonical products only)
	•	Duplicate detection
	•	Discount logic per damage condition
	•	Variant structure
	•	Shopify writes
	•	Logging and audit guarantees

Rule: If DBS didn’t compute it, it cannot be executed.

⸻

2. Two-Phase Model (Preview → Confirm)

All damaged product creation follows two distinct phases.

Phase 1 — Preview (Read-Only)

Endpoint:

POST /admin/bulk-create/preview

Characteristics:
	•	Zero Shopify writes
	•	Fully repeatable
	•	Safe to retry
	•	Returns exactly what would be created

AD must:
	•	Treat preview output as immutable truth
	•	Render preview without modification
	•	Block confirm if preview fails

⸻

Phase 2 — Confirm (Irreversible)

Endpoint:

POST /admin/bulk-create/confirm

Characteristics:
	•	Performs irreversible Shopify writes
	•	Executes only preview-derived payloads
	•	Cannot accept raw inputs (ISBNs, product IDs, etc.)

AD must:
	•	Derive confirm payload only from preview response
	•	Never recompute inventory, discounts, or variants
	•	Never retry automatically

⸻

3. Preview Request Contract

Request Shape (AD → DBS)

{
  "inputs": [
    { "type": "isbn", "value": "9782379450907" },
    { "type": "product_id", "value": "1234567890" }
  ],
  "inventory": {
    "light": 5,
    "moderate": 0,
    "heavy": 0
  }
}

Rules:
	•	inputs[] may be space-, comma-, or CSV-derived
	•	inventory is a seed, not a guarantee
	•	Inventory is applied once at creation time only

⸻

4. Preview Response Contract

Response Shape (DBS → AD)

type PreviewItem = {
  canonical: {
    product_id: number;
    title: string;
    handle: string;
  };
  damaged_product: {
    handle: string;
    title: string;
    variants: {
      condition: 'light' | 'moderate' | 'heavy';
      price_modifier: number; // percent off
      inventory: number;
    }[];
  };
};

Guarantees:
	•	One preview item = one canonical product
	•	Variants are precomputed
	•	Discounts are final
	•	AD must not alter or infer anything

⸻

5. Confirm Request Contract (Critical)

Request Shape (AD → DBS)

class BulkCreateConfirmRequest {
  items: {
    canonical_handle: string;
    inventory: {
      light: number;
      moderate: number;
      heavy: number;
    };
  }[];
}

Hard rules:
	•	Confirm does not accept ISBNs or product IDs
	•	Confirm does not re-resolve Shopify data
	•	Confirm executes only what preview computed

If the preview was wrong, the fix is to re-run preview — never to “fix” confirm.

⸻

6. Explicit Non-Responsibilities (By Design)

AD Must Never:
	•	Create damaged products in Shopify Admin
	•	Modify damaged product titles, handles, or descriptions
	•	Create or edit damaged variants
	•	Manually apply discounts
	•	Adjust damaged inventory outside DBS
	•	Bypass preview

DBS Must Never:
	•	Accept confirm payloads that were not preview-derived
	•	Infer missing inventory or conditions
	•	Guess canonical products
	•	Allow damaged products as canonical inputs

⸻

7. Why This Ceremony Exists

Shopify provides no failsafe against:
	•	Duplicate product creation
	•	Conflicting variant structures
	•	Inventory misalignment

DBS exists specifically to provide:
	•	Deterministic creation
	•	Auditable logs
	•	Safe retries
	•	Hard validation boundaries

The Admin Dashboard is intentionally constrained to prevent accidental misuse.

⸻

8. Mental Model for Engineers

Think of DBS as:

“A database with side effects.”

Think of AD as:

“A read-only terminal until the final confirm button is pressed.”

If you ever feel tempted to “just add a small check” on the AD side — stop.
That logic belongs in DBS.

⸻

9. If You’re Unsure

Ask:
	1.	Did DBS compute this?
	2.	Is this preview-derived?
	3.	Would retrying this be dangerous?

If any answer is unclear, do not implement until clarified.

⸻

Status

✅ Contract locked
✅ Backend enforced
✅ Admin aligned
✅ Safe for CSV expansion
✅ Safe for future automation