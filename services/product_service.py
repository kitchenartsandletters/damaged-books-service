# services/product_service.py

import logging
from datetime import datetime
from services.shopify_client import shopify_client
from services.supabase_client import supabase_client
from backend.app.schemas import (
    DuplicateCheckRequest,
    DuplicateCheckResponse,
    BulkCreateRequest,
    BulkCreateResult,
    CanonicalProductInfo,
    VariantSeed,
    CreatedVariantInfo,
)
from services.creation_log_service import log_creation_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Variant helpers & condition metadata
# ---------------------------------------------------------------------------

CONDITION_META = {
    "light": {
        "title": "Light Damage",
        "default_discount": 0.15,
    },
    "moderate": {
        "title": "Moderate Damage",
        "default_discount": 0.30,
    },
    "heavy": {
        "title": "Heavy Damage",
        "default_discount": 0.60,
    },
}

def _snake_handle(handle: str) -> str:
    """
    Convert canonical handle into a snake_case-ish base for synthetic barcodes.
    Example: 'pride-and-prejudice' -> 'pride_and_prejudice'
    """
    h = (handle or "").strip().lower()
    return h.replace("-", "_").replace(" ", "_")

def _make_barcode_for_condition(canonical_handle: str, condition_key: str) -> str:
    """
    Synthetic barcode: <snake_handle>_<condition_key>
    Example: 'pride-and-prejudice', 'light' -> 'pride_and_prejudice_light'
    """
    base = _snake_handle(canonical_handle)
    return f"{base}_{condition_key}"

def _normalize_condition_from_title(title: str | None) -> str | None:
    """
    Map variant title (e.g. 'Light Damage') back to canonical condition keys:
    'light', 'moderate', 'heavy'.
    """
    t = (title or "").lower()
    if "light" in t:
        return "light"
    if "moderate" in t or "mod " in t:
        return "moderate"
    if "heavy" in t:
        return "heavy"
    return None

async def find_existing_by_handle(handle: str) -> dict | None:
    """
    Return first Shopify product that matches handle (exact) OR Shopify's auto-suffixed variants
    such as '-1', '-2', etc.
    """
    try:
        # Query Shopify for products with the given handle or auto-suffixed versions
        base = handle.lower().strip()
        suffix_candidates = [base, f"{base}-1", f"{base}-2", f"{base}-3"]
        for h in suffix_candidates:
            resp = await shopify_client.get("products.json", query={"handle": h})
            products = resp.get("body", {}).get("products", [])
            if products:
                return products[0]
        return None
    except Exception as e:
        logger.warning(f"[DuplicateCheck] Failed finding existing product by handle={handle}: {e}")
        return None




async def check_damaged_duplicate(
    canonical_handle: str,
    damaged_handle: str
) -> dict:
    """
    Comprehensive duplicate check used by the Admin Dashboard wizard.

    Returns a structured result:
    {
        "status": "ok" | "conflict",
        "canonical_handle": "...",
        "damaged_handle": "...",
        "conflicts": {
            "canonical_exists": bool,
            "damaged_exists": bool,
            "canonical_variants": [...],
            "damaged_variants": [...],
            "handle_collision": bool,
        },
        "existing_products": {
            "canonical": <product or None>,
            "damaged": <product or None>
        },
        "inventory_rows": [...],   # from Supabase
        "safe_to_create": bool
    }
    """

    result = {
        "status": "ok",
        "canonical_handle": canonical_handle,
        "damaged_handle": damaged_handle,
        "conflicts": {
            "canonical_exists": False,
            "damaged_exists": False,
            "canonical_variants": [],
            "damaged_variants": [],
            "handle_collision": False,
        },
        "existing_products": {
            "canonical": None,
            "damaged": None
        },
        "inventory_rows": [],
        "safe_to_create": True
    }

    try:
        base_canonical = canonical_handle.lower().strip()
        base_damaged = damaged_handle.lower().strip()

        # ------------------------------------------------------------------
        # 1. Shopify canonical product search (including suffixes)
        # ------------------------------------------------------------------
        canonical_candidates = [
            base_canonical,
            f"{base_canonical}-1",
            f"{base_canonical}-2",
            f"{base_canonical}-3"
        ]

        found_canonical = None
        for h in canonical_candidates:
            resp = await shopify_client.get("products.json", query={"handle": h})
            items = resp.get("body", {}).get("products", [])
            if items:
                found_canonical = items[0]
                break

        if found_canonical:
            result["conflicts"]["canonical_exists"] = True
            result["existing_products"]["canonical"] = found_canonical
            result["conflicts"]["canonical_variants"] = [
                v.get("id") for v in found_canonical.get("variants", [])
            ]

        # ------------------------------------------------------------------
        # 2. Shopify damaged product search (including suffixes)
        # ------------------------------------------------------------------
        damaged_candidates = [
            base_damaged,
            f"{base_damaged}-1",
            f"{base_damaged}-2",
            f"{base_damaged}-3"
        ]

        found_damaged = None
        for h in damaged_candidates:
            resp = await shopify_client.get("products.json", query={"handle": h})
            items = resp.get("body", {}).get("products", [])
            if items:
                found_damaged = items[0]
                break

        if found_damaged:
            result["conflicts"]["damaged_exists"] = True
            result["existing_products"]["damaged"] = found_damaged
            result["conflicts"]["damaged_variants"] = [
                v.get("id") for v in found_damaged.get("variants", [])
            ]

        # ------------------------------------------------------------------
        # 3. Handle collision check (canonical-damaged root mismatch)
        # ------------------------------------------------------------------
        if found_damaged and found_canonical:
            result["conflicts"]["handle_collision"] = True

        # ------------------------------------------------------------------
        # 4. Supabase inventory check for damaged products sharing this root
        # ------------------------------------------------------------------
        try:
            # inventory_view includes fields:
            # product_id, handle, condition, available, etc.
            supabase = await supabase_client.get_client()
            rows = (
                supabase.table("inventory_view")
                .select("*")
                .ilike("handle", f"%{base_damaged}%")
                .execute()
            )
            result["inventory_rows"] = rows.data or []
        except Exception as e:
            logger.warning(f"[check_damaged_duplicate] Supabase inventory fetch failed: {e}")

        # ------------------------------------------------------------------
        # 5. Final resolution logic
        # ------------------------------------------------------------------
        has_conflict = any([
            result["conflicts"]["canonical_exists"],
            result["conflicts"]["damaged_exists"],
            result["conflicts"]["handle_collision"],
            len(result["inventory_rows"]) > 0
        ])

        if has_conflict:
            result["status"] = "conflict"
            result["safe_to_create"] = False

        return result

    except Exception as e:
        logger.error(f"[check_damaged_duplicate] Failure: {e}")
        return {
            "status": "error",
            "error": str(e),
            "safe_to_create": False
        }

async def create_damaged_product_with_duplicate_check(
    data: BulkCreateRequest
) -> BulkCreateResult:
    """
    Duplicate-check → optional dry-run → create damaged product → return BulkCreateResult.

    Variant-level controls:
      - data.variants: list[VariantSeed] with:
          condition: 'light' | 'moderate' | 'heavy'
          quantity: optional int (purely reported for now)
          price_override: optional float (percentage OFF; 0.25 == 25% off)
      - compare_at_price is currently ignored at this phase.
    """

    # Build a quick lookup map for variant seeds keyed by condition
    seed_by_condition: dict[str, VariantSeed] = {}
    for seed in data.variants or []:
        key = (seed.condition or "").strip().lower()
        if key:
            seed_by_condition[key] = seed

    # -------------------------------------------------------
    # Step 1: Duplicate check
    # -------------------------------------------------------
    dup_result = await check_damaged_duplicate(
        canonical_handle=data.canonical_handle,
        damaged_handle=data.damaged_handle,
    )

    if dup_result.get("status") != "ok" or not dup_result.get("safe_to_create", False):
        logger.warning(f"[BulkCreate] Conflict detected for {data.damaged_handle}")

        result = BulkCreateResult(
            status="error",
            damaged_product_id=None,
            damaged_handle=data.damaged_handle,
            variants=[],
            messages=["Duplicate or conflict detected; creation aborted."],
        )

        # Log (Option A — log everything)
        try:
            await log_creation_event(data, result, operator=None)
        except Exception as e:
            logger.warning(f"[CreationLog] Failed to write conflict log: {e}")

        return result

    # -------------------------------------------------------
    # Step 2: Dry-run mode (no Shopify mutation)
    # -------------------------------------------------------
    if data.dry_run:
        logger.info(f"[BulkCreate] Dry run for {data.damaged_handle}")

        result = BulkCreateResult(
            status="dry-run",
            damaged_product_id=None,
            damaged_handle=data.damaged_handle,
            variants=[],
            messages=["Dry run: no product created."],
        )

        try:
            await log_creation_event(data, result, operator=None)
        except Exception as e:
            logger.warning(f"[CreationLog] Failed to write dry-run log: {e}")

        return result

    # -------------------------------------------------------
    # Step 3: Create damaged product via create_damaged_pair()
    # DBS NEVER creates canonical products.
    # We pass variant seeds so create_damaged_pair can apply price overrides.
    # -------------------------------------------------------
    created = await create_damaged_pair(
        canonical_title=data.canonical_title,
        canonical_handle=data.canonical_handle,
        damaged_title=data.damaged_title,
        damaged_handle=data.damaged_handle,
        isbn=data.isbn,
        barcode=data.barcode,
        variants=data.variants,  # <-- NEW: pass VariantSeed list
    )

    damaged = created.get("damaged", {}) or {}
    damaged_id = damaged.get("id")
    damaged_handle = damaged.get("handle")

    # -------------------------------------------------------
    # Step 4: Extract CreatedVariantInfo[]
    # (Bulk-create does NOT mutate inventory yet → quantity_set reports the
    #  requested quantity but does not adjust Shopify inventory at this phase.)
    # -------------------------------------------------------
    extracted_variants: list[CreatedVariantInfo] = []

    for v in damaged.get("variants", []) or []:
        title = v.get("title")
        cond_key = _normalize_condition_from_title(title)
        qty = 0

        if cond_key and cond_key in seed_by_condition:
            seed = seed_by_condition[cond_key]
            if seed.quantity is not None:
                try:
                    qty = int(seed.quantity)
                except Exception:
                    qty = 0

        extracted_variants.append(
            CreatedVariantInfo(
                condition=title,
                variant_id=v.get("id"),
                quantity_set=qty,
                price=float(v.get("price") or 0),
                sku=v.get("sku"),
                barcode=v.get("barcode"),
                inventory_management=v.get("inventory_management"),
                inventory_policy=v.get("inventory_policy"),
            )
        )

    # -------------------------------------------------------
    # Step 5: Build final BulkCreateResult
    # -------------------------------------------------------
    result = BulkCreateResult(
        status="created",
        damaged_product_id=damaged_id,
        damaged_handle=damaged_handle,
        variants=extracted_variants,
        messages=["Damaged product created successfully."],
    )

    # -------------------------------------------------------
    # Step 6: Write to creation_log (Option A: log all runs)
    # -------------------------------------------------------
    try:
        await log_creation_event(data, result, operator=None)
    except Exception as e:
        logger.warning(f"[CreationLog] Failed to write create log: {e}")

    return result

def parse_damaged_handle(handle: str) -> tuple[str, str]:
    """
    Deprecated: Legacy function to parse damaged book handles.
    Use `used_book_manager` and `inventory_service` instead.
    """
    logger.warning("parse_damaged_handle is deprecated. Use 'used_book_manager' and 'inventory_service' instead.")
    import re
    h = (handle or "").lower()
    # Legacy: <base>-(hurt|used|damaged|damage)-(light|moderate|mod|heavy)
    m = re.match(r"^(?P<base>.+)-(?:hurt|used|damaged|damage)-(light|moderate|mod|heavy)$", h)
    if m:
        return m.group("base"), m.group(2)
    # New: <base>-(light|moderate|mod|heavy)-damage
    m = re.match(r"^(?P<base>.+)-(light|moderate|mod|heavy)-damage$", h)
    if m:
        return m.group("base"), m.group(2)
    return handle, None

def is_used_book_handle(handle: str) -> bool:
    """
    Deprecated: Check if handle is for a used book.
    Use `used_book_manager` and `inventory_service` instead.
    """
    logger.warning("is_used_book_handle is deprecated. Use 'used_book_manager' and 'inventory_service' instead.")
    base, condition = parse_damaged_handle(handle)
    return condition is not None

def get_new_book_handle_from_used(used_handle: str) -> str:
    """
    Deprecated: Extract new book handle from a used book handle.
    Use `used_book_manager` and `inventory_service` instead.
    """
    logger.warning("get_new_book_handle_from_used is deprecated. Use 'used_book_manager' and 'inventory_service' instead.")
    base, condition = parse_damaged_handle(used_handle)
    return base

def parse_condition_from_handle(handle: str) -> str | None:
    """
    Deprecated: Parse condition from a book handle.
    Use `used_book_manager` and `inventory_service` instead.
    """
    logger.warning("parse_condition_from_handle is deprecated. Use 'used_book_manager' and 'inventory_service' instead.")
    _, cond = parse_damaged_handle(handle)
    return cond

def is_damaged_handle(handle: str) -> bool:
    """Preferred function to check if a handle is for a damaged or used book."""
    return is_used_book_handle(handle)


async def create_damaged_pair(
    canonical_title: str,
    canonical_handle: str,
    damaged_title: str,
    damaged_handle: str,
    isbn: str | None = None,
    barcode: str | None = None,
    variants: list[VariantSeed] | None = None,
) -> dict:
    """
    Create damaged companion product only, based on an existing canonical product.
    DBS NEVER creates canonical products.

    Variant behavior:
      - Always creates 3 condition variants: Light / Moderate / Heavy Damage.
      - Default discounts (off canonical price):
          light   = 15%
          moderate= 30%
          heavy   = 60%
      - If VariantSeed.price_override is provided for a condition:
          price = canonical_price * (1 - price_override)
        (Option A: percentage OFF)
      - SKU: canonical variant sku (author) is copied to all damaged variants.
      - Barcode: synthetic, not inherited:
          snake_case(canonical_handle) + '_' + condition_key
          e.g. 'pride_and_prejudice_light'
      - Inventory_management: 'shopify'
      - Inventory_policy: 'deny' (no overselling of damaged books)
    """

    logger.info(f"[CreateDamagedPair] canonical={canonical_handle}, damaged={damaged_handle}")

    # 1. Fetch canonical
    canonical = await find_existing_by_handle(canonical_handle)
    if not canonical:
        raise RuntimeError(f"Canonical product not found for handle '{canonical_handle}'.")

    canonical_variant = (canonical.get("variants") or [{}])[0]
    canonical_price_raw = canonical_variant.get("price") or "0.00"

    try:
        canonical_price = float(canonical_price_raw)
    except Exception:
        canonical_price = 0.0

    canonical_barcode = canonical_variant.get("barcode")  # no longer used directly
    canonical_sku = canonical_variant.get("sku")          # DBS rule: sku = author; we inherit this
    vendor = canonical.get("vendor")
    product_type = canonical.get("product_type")
    canonical_tags = canonical.get("tags", [])
    canonical_images = canonical.get("images", [])
    canonical_image_src = canonical_images[0]["src"] if canonical_images else None

    # Build a quick lookup for VariantSeed by condition (light/moderate/heavy)
    seed_by_condition: dict[str, VariantSeed] = {}
    for seed in variants or []:
        key = (seed.condition or "").strip().lower()
        if key:
            seed_by_condition[key] = seed

    # 2. Static damaged copy (MUST NOT BE CHANGED)
    damaged_preamble = (
        "We received some copies of this book which are less than perfect. "
        "While supplies last, we are offering these copies at a reduced price. "
        "These copies are first come, first served: we cannot reserve one for you. "
        "And we cannot predict when we might receive more once we sell out of them, "
        "mostly because we really don't want to get any more damaged copies. 😊\n"
        "When you order one of these books, we'll use our judgment to choose the best remaining "
        "copy for you in the category you choose. Purchases of damaged books are final sales: "
        "they are not refundable or exchangeable."
    )

    # 3. SEO
    seo_title = f"{canonical_title} (Damaged)"
    seo_description = damaged_preamble

    # 4. Discount + price override logic (Option A: percentage OFF)
    def compute_price_for_condition(cond_key: str) -> str:
        meta = CONDITION_META.get(cond_key)
        if not meta:
            return canonical_price_raw

        default_pct = meta["default_discount"]

        override_pct: float | None = None
        seed = seed_by_condition.get(cond_key)
        if seed and seed.price_override is not None:
            try:
                override_pct = float(seed.price_override)
            except Exception:
                override_pct = None

        pct = override_pct if override_pct is not None else default_pct

        try:
            return f"{canonical_price * (1 - pct):.2f}"
        except Exception:
            return canonical_price_raw

    # 5. Build variant payloads (3 condition variants)
    variant_payloads: list[dict] = []

    for cond_key in ("light", "moderate", "heavy"):
        meta = CONDITION_META[cond_key]
        title = meta["title"]

        variant_payloads.append(
            {
                "title": title,
                "option1": title,
                "sku": canonical_sku or "",
                "barcode": _make_barcode_for_condition(canonical_handle, cond_key),
                "price": compute_price_for_condition(cond_key),
                "inventory_management": "shopify",
                "inventory_policy": "deny",  # Damaged books NEVER continue selling
            }
        )

    # 6. Build product payload
    payload = {
        "product": {
            "title": damaged_title,
            "handle": damaged_handle,
            "status": "draft",  # created as draft; publishing is handled elsewhere
            "body_html": damaged_preamble,
            "vendor": vendor,
            "product_type": product_type,
            "tags": list(set(canonical_tags + ["damaged"])),
            "images": [{"src": canonical_image_src}] if canonical_image_src else [],
            "metafields": [
                {
                    "namespace": "custom",
                    "key": "canonical_handle",
                    "value": canonical_handle,
                    "type": "single_line_text_field",
                }
            ],
            "options": [
                {
                    "name": "Condition",
                    "values": [
                        CONDITION_META["light"]["title"],
                        CONDITION_META["moderate"]["title"],
                        CONDITION_META["heavy"]["title"],
                    ],
                }
            ],
            "variants": variant_payloads,
            "seo": {"title": seo_title, "description": seo_description},
        }
    }

    # 7. Create damaged product
    resp = await shopify_client.post("products.json", data=payload)
    damaged = resp.get("body", {}).get("product")
    if not damaged:
        raise RuntimeError(f"Failed to create damaged product {damaged_handle}")

    logger.info(f"[CreateDamagedPair] Created damaged id={damaged.get('id')}")

    return {"canonical": canonical, "damaged": damaged}


# --------------------------------------------------------
# _publish_to_online_store and _unpublish_from_online_store are not referenced in routes.py or used_book_manager.py (both use set_product_publish_status).
# Decision point: keep them for future fallback OR remove if we want to enforce one publishing pathway.
# --------------------------------------------------------

# ⬇️ make async and await the client
async def get_product_by_id(product_id: str) -> dict:
    """
    Fetch full Shopify product object by ID.
    """
    try:
        path = f"products/{product_id}.json"
        response = await shopify_client.get(path)
        # response shape: { "status": int, "body": dict, "headers": dict }
        return response.get("body", {}).get("product", {})
    except Exception as e:
        logger.error(f"Error fetching product {product_id}: {str(e)}")
        raise

# ⬇️ make async and await the client
async def set_product_publish_status(product_id: str, should_publish: bool) -> dict:
    """
    Publish or unpublish a Shopify product by setting `published_at`.
    """
    try:
        published_at = datetime.utcnow().isoformat() if should_publish else None
        path = f"products/{product_id}.json"
        payload = {
            "product": {
                "id": product_id,
                "published_at": published_at
            }
        }
        response = await shopify_client.put(path, data=payload)
        return response.get("body", {}).get("product", {})
    except Exception as e:
        action = "publishing" if should_publish else "unpublishing"
        logger.error(f"Error {action} product {product_id}: {str(e)}")
        raise