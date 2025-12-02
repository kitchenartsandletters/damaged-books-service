# backend/app/schemas.py

from typing import Optional, List, Dict
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# SHARED: Canonical Product Snapshot (read-only)
# Only the fields we allow to be inherited into damaged product creation.
# ---------------------------------------------------------------------------

class CanonicalProductInfo(BaseModel):
    product_id: str
    handle: str
    title: str
    vendor: Optional[str] = None
    product_type: Optional[str] = None
    tags: List[str] = []
    price: Optional[float] = None       # canonical base price for computing discounts
    weight: Optional[float] = None
    barcode: Optional[str] = None
    sku: Optional[str] = None           # DBS rule: canonical sku = author
    image_src: Optional[str] = None     # ONLY first image (cover)


# ---------------------------------------------------------------------------
# DUPLICATE CHECK: Request
# Used by /admin/check-duplicate
# ---------------------------------------------------------------------------

class DuplicateCheckRequest(BaseModel):
    canonical_handle: str
    canonical_title: str
    damaged_handle: str
    damaged_title: str
    isbn: Optional[str] = None
    barcode: Optional[str] = None


# ---------------------------------------------------------------------------
# DUPLICATE CHECK: Response (structured conflicts)
# ---------------------------------------------------------------------------

class DuplicateConflict(BaseModel):
    conflict_type: str                     # "canonical_exists", "damaged_exists", "handle_conflict", …
    message: str
    existing_product_id: Optional[str] = None
    existing_handle: Optional[str] = None


class DuplicateCheckResponse(BaseModel):
    status: str                             # "ok" or "conflicts"
    conflicts: List[DuplicateConflict] = []
    canonical: Optional[CanonicalProductInfo] = None
    damaged_handle_sanitized: Optional[str] = None


# ---------------------------------------------------------------------------
# BULK CREATE: Variant injection
# ---------------------------------------------------------------------------

class VariantSeed(BaseModel):
    condition: str                          # "light", "moderate", "heavy"
    quantity: Optional[int] = 0             # user optional input
    price_override: Optional[float] = None  # user may override %
    compare_at_price: Optional[float] = None


# ---------------------------------------------------------------------------
# BULK CREATE: Request to /admin/bulk-create
# ---------------------------------------------------------------------------

class BulkCreateRequest(BaseModel):
    canonical_handle: str
    canonical_title: str
    damaged_handle: str
    damaged_title: str
    isbn: Optional[str] = None
    barcode: Optional[str] = None

    variants: List[VariantSeed] = Field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# BULK CREATE: Response to Admin Dashboard
# ---------------------------------------------------------------------------

class CreatedVariantInfo(BaseModel):
    condition: str
    variant_id: Optional[str]
    quantity_set: Optional[int]
    price: Optional[float]
    sku: Optional[str]
    barcode: Optional[str]
    inventory_management: Optional[str]
    inventory_policy: Optional[str]


class BulkCreateResult(BaseModel):
    status: str
    damaged_product_id: Optional[str] = None
    damaged_handle: Optional[str] = None
    variants: List[CreatedVariantInfo] = []
    messages: List[str] = []