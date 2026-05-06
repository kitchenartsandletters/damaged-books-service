# backend/app/schemas.py

from typing import Optional, List, Dict, Literal, Union
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# SHARED: Canonical Product Snapshot (read-only)
# ---------------------------------------------------------------------------

class CanonicalProductInfo(BaseModel):
    product_id: str
    handle: str
    title: str
    vendor: Optional[str] = None
    product_type: Optional[str] = None
    tags: List[str] = []
    price: Optional[float] = None
    weight: Optional[float] = None
    barcode: Optional[str] = None
    sku: Optional[str] = None
    image_src: Optional[str] = None


# ---------------------------------------------------------------------------
# DUPLICATE CHECK
# ---------------------------------------------------------------------------

class DuplicateCheckRequest(BaseModel):
    canonical_handle: str


class DuplicateConflict(BaseModel):
    conflict_type: str
    message: str
    existing_product_id: Optional[str] = None
    existing_handle: Optional[str] = None


class DuplicateCheckResponse(BaseModel):
    status: str
    conflicts: List[DuplicateConflict] = []
    canonical: Optional[CanonicalProductInfo] = None
    damaged_handle_sanitized: Optional[str] = None


# ---------------------------------------------------------------------------
# BULK CREATE: Input and Inventory Seed Models
# ---------------------------------------------------------------------------

class BulkCreateInput(BaseModel):
    type: str  # "isbn" | "product_id" — used for logging only; backend cascades
    value: str


class InventorySeed(BaseModel):
    light: int = 0
    moderate: int = 0
    heavy: int = 0


class VariantSeed(BaseModel):
    condition: str
    quantity: Optional[int] = 0
    price_override: Optional[float] = None


# ---------------------------------------------------------------------------
# BULK CREATE: Request
# ---------------------------------------------------------------------------

class BulkCreateRequest(BaseModel):
    # New bulk interface (preferred)
    inputs: Optional[List[BulkCreateInput]] = None
    inventory: Optional[InventorySeed] = None

    # Legacy single-canonical interface (deprecated)
    canonical_handle: Optional[str] = None
    variants: List[VariantSeed] = Field(default_factory=list)

    dry_run: bool = False


# ---------------------------------------------------------------------------
# BULK CREATE: Response
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
# ---------------------------------------------------------------------------

class BulkCreateConfirmItem(BaseModel):
    """
    A single damaged variant to be created or updated.
    Derived verbatim from preview output.

    canonical_product_id accepts both int and string — the frontend sends
    the numeric ID as a string; Pydantic coerces it automatically.
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

    @field_validator("canonical_product_id", mode="before")
    @classmethod
    def coerce_product_id(cls, v):
        """
        Accept numeric strings and full GIDs from the frontend.
        e.g. "6831066579077" or "gid://shopify/Product/6831066579077" → 6831066579077
        """
        if isinstance(v, str):
            raw = v.split("/")[-1]
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f"canonical_product_id must be numeric, got: {v!r}")
        return v


class BulkCreateConfirmRequest(BaseModel):
    """
    Confirm request for /admin/bulk-create.
    Flat list: one item per condition per book.
    """

    items: List[BulkCreateConfirmItem] = Field(
        ...,
        min_length=1,
        description="Preview-derived items to execute"
    )