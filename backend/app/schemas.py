# backend/app/schemas.py

from typing import Optional, List, Dict, Literal
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
# BULK CREATE: Input and Inventory Seed Models
# ---------------------------------------------------------------------------

class BulkCreateInput(BaseModel):
    type: str  # "isbn" | "product_id"
    value: str


class InventorySeed(BaseModel):
    light: int = 0
    moderate: int = 0
    heavy: int = 0

# ---------------------------------------------------------------------------
# BULK CREATE: Variant injection
# ---------------------------------------------------------------------------

class VariantSeed(BaseModel):
    condition: str                          # "light", "moderate", "heavy"
    quantity: Optional[int] = 0             # user optional input
    price_override: Optional[float] = None  # user may override %

# ---------------------------------------------------------------------------
# BULK CREATE: Request to /admin/bulk-create
# ---------------------------------------------------------------------------


# BulkCreateRequest: `inputs` + `inventory` is canonical, `canonical_handle` is legacy
class BulkCreateRequest(BaseModel):
    # New bulk interface (preferred)
    inputs: Optional[List[BulkCreateInput]] = None
    inventory: Optional[InventorySeed] = None

    # Legacy single-canonical interface (deprecated)
    canonical_handle: Optional[str] = None
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

# ---------------------------------------------------------------------------
# BULK CREATE: Confirm (execution-only)
# Preview-derived payload ONLY
# ---------------------------------------------------------------------------

class BulkCreateConfirmItem(BaseModel):
    """
    A single damaged variant to be created.
    This must be derived verbatim from preview output.
    """

    canonical_product_id: int = Field(
        ...,
        description="Shopify product ID of the canonical (single-variant) product"
    )

    canonical_handle: str = Field(
        ...,
        description="Handle of the canonical product (for logging + validation only)"
    )

    condition_key: Literal["light", "moderate", "heavy"] = Field(
        ...,
        description="Damage condition to create"
    )

    inventory: int = Field(
        ...,
        ge=0,
        description="Inventory quantity to seed for this damaged variant"
    )


class BulkCreateConfirmRequest(BaseModel):
    """
    Confirm request for /admin/bulk-create.
    Executes ONLY what preview already computed.
    """

    items: List[BulkCreateConfirmItem] = Field(
        ...,
        min_items=1,
        description="Preview-derived items to execute"
    )