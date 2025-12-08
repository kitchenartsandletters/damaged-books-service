# services/product_service.py

import logging
import os
from datetime import datetime
from services.shopify_client import shopify_client
from services.supabase_client import get_client
from backend.app.schemas import (
    BulkCreateRequest,
    BulkCreateResult,
    VariantSeed,
    CreatedVariantInfo,
)
from services.creation_log_service import log_creation_event

logger = logging.getLogger(__name__)

# Shopify location used when setting damaged inventory levels.
# Preferred: DBS_SHOPIFY_LOCATION_ID (service-specific); fallback: SHOPIFY_LOCATION_ID.
_SHOPIFY_LOCATION_ID_RAW = os.getenv("DBS_SHOPIFY_LOCATION_ID") or os.getenv("SHOPIFY_LOCATION_ID")
try:
    SHOPIFY_LOCATION_ID: int | None = int(_SHOPIFY_LOCATION_ID_RAW) if _SHOPIFY_LOCATION_ID_RAW else None
except ValueError:
    SHOPIFY_LOCATION_ID = None
    logger.warning(
        "[InventoryUpdate] Invalid DBS_SHOPIFY_LOCATION_ID/SHOPIFY_LOCATION_ID value: %r",
        _SHOPIFY_LOCATION_ID_RAW,
    )

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
    Return first Shopify product that looks like it matches the given handle.

    Strategy:
      1. Try the exact handle and a few auto-suffix candidates: base, base-1, base-2, base-3.
      2. If nothing found, fall back to a brute-force scan of up to 250 products and
         return the first whose handle == base or startswith(f"{base}-").
    """
    try:
        base = (handle or "").strip().lower()

        # ----------------------------------------------------
        # Phase 1: direct handle candidates (base, base-1, ...)
        # ----------------------------------------------------
        suffix_candidates = [base, f"{base}-1", f"{base}-2", f"{base}-3"]
        for h in suffix_candidates:
            resp = await shopify_client.get("products.json", query={"handle": h})
            products = resp.get("body", {}).get("products", [])
            if products:
                return products[0]

        # ----------------------------------------------------
        # Phase 2: brute-force fallback (up to 250 products)
        # ----------------------------------------------------
        # Some stores / API behaviors ignore the ?handle= filter or don't surface
        # the product via that parameter. As a safety net, load a page of products
        # and match by handle prefix.
        resp = await shopify_client.get("products.json", query={"limit": 250})
        all_products = resp.get("body", {}).get("products", []) or []

        for p in all_products:
            h = (p.get("handle") or "").strip().lower()
            if h == base or h.startswith(f"{base}-"):
                logger.info(
                    "[find_existing_by_handle] Fallback match: requested=%s matched=%s",
                    base,
                    h,
                )
                return p

        # Nothing found
        logger.info(
            "[find_existing_by_handle] No product found for handle base='%s' after suffix + fallback scan",
            base,
        )
        return None

    except Exception as e:
        logger.warning(
            "[DuplicateCheck] Failed finding existing product by handle=%s: %s",
            handle,
            e,
        )
        return None

async def check_damaged_duplicate(
    canonical_handle: str,
    damaged_handle: str,
) -> dict:
    """
    NEW DUPLICATE-CHECK LOGIC (2025 REBUILD)

    Rules:
      - Canonical product MUST exist → if missing → conflict
      - Damaged product (auto handle: canonical + "-damaged") MUST NOT exist
      - Any damaged inventory rows in Supabase → conflict
      - canonical_exists is informational only, not a conflict
    """

    logger.info(
        f"[DuplicateCheck] Checking canonical='{canonical_handle}', "
        f"damaged='{damaged_handle}'"
    )

    result = {
        "status": "ok",
        "canonical_handle": canonical_handle,
        "damaged_handle": damaged_handle,
        "conflicts": {
            "canonical_missing": False,   # NEW field
            "damaged_exists": False,
            "inventory_present": False,
        },
        "existing_products": {
            "canonical": None,
            "damaged": None,
        },
        "inventory_rows": [],
        "safe_to_create": True
    }

    try:
        base_canonical = canonical_handle.strip().lower()
        base_damaged   = damaged_handle.strip().lower()

        # --------------------------------------------------------
        # 1. FIND CANONICAL (REQUIRED)
        # --------------------------------------------------------
        found_canonical = await find_existing_by_handle(base_canonical)

        if not found_canonical:
            logger.warning(
                f"[DuplicateCheck] Canonical NOT FOUND for '{base_canonical}'"
            )
            result["conflicts"]["canonical_missing"] = True
        else:
            result["existing_products"]["canonical"] = found_canonical

        # --------------------------------------------------------
        # 2. FIND DAMAGED (MUST NOT EXIST)
        # --------------------------------------------------------
        found_damaged = await find_existing_by_handle(base_damaged)

        if found_damaged:
            logger.warning(
                f"[DuplicateCheck] Damaged ALREADY EXISTS for '{base_damaged}'"
            )
            result["conflicts"]["damaged_exists"] = True
            result["existing_products"]["damaged"] = found_damaged

        # --------------------------------------------------------
        # 3. SUPABASE INVENTORY CHECK (MUST BE CLEAN)
        # --------------------------------------------------------
        try:
            supabase = get_client()
            rows = (
                supabase.schema("damaged")
                .table("inventory_view")
                .select("*")
                .ilike("handle", f"%{base_damaged}%")
                .execute()
            )
            inv = rows.data or []
            result["inventory_rows"] = inv

            if inv:
                result["conflicts"]["inventory_present"] = True
                logger.warning(
                    "[DuplicateCheck] Inventory rows found for damaged root → conflict"
                )
        except Exception as e:
            logger.warning(f"[DuplicateCheck] Supabase error: {e}")

        # --------------------------------------------------------
        # 4. FINAL RESOLUTION
        # --------------------------------------------------------
        has_conflict = any([
            result["conflicts"]["canonical_missing"],   # must NOT happen
            result["conflicts"]["damaged_exists"],      # must NOT happen
            result["conflicts"]["inventory_present"],   # must NOT happen
        ])

        if has_conflict:
            result["status"] = "conflict"
            result["safe_to_create"] = False
            logger.info(f"[DuplicateCheck] → CONFLICT for '{damaged_handle}'")
        else:
            logger.info(f"[DuplicateCheck] → CLEAR TO CREATE '{damaged_handle}'")

        return result

    except Exception as e:
        logger.error(f"[DuplicateCheck] Fatal error: {e}")
        return {
            "status": "error",
            "error": str(e),
            "safe_to_create": False,
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
    # Step 1: Compute auto_damaged_handle
    # -------------------------------------------------------
    # Damaged title is NOT derived here — handled exclusively inside create_damaged_pair()
    auto_damaged_handle = f"{data.canonical_handle.strip().lower()}-damaged"

    # -------------------------------------------------------
    # Step 2: Duplicate check (use auto_damaged_handle)
    # -------------------------------------------------------
    dup_result = await check_damaged_duplicate(
        canonical_handle=data.canonical_handle,
        damaged_handle=auto_damaged_handle,
    )

    if dup_result.get("status") != "ok" or not dup_result.get("safe_to_create", False):
        logger.warning(f"[BulkCreate] Conflict detected for {auto_damaged_handle}")

        result = BulkCreateResult(
            status="error",
            damaged_product_id=None,
            damaged_handle=auto_damaged_handle,
            variants=[],
            messages=["Duplicate or conflict detected; creation aborted."],
        )

        try:
            await log_creation_event(data, result)
        except Exception as e:
            logger.warning(f"[CreationLog] Failed to write conflict log: {e}")

        return result

    # -------------------------------------------------------
    # Step 3: Dry-run mode (no Shopify mutation)
    # -------------------------------------------------------
    if data.dry_run:
        logger.info(f"[BulkCreate] Dry run for {auto_damaged_handle}")

        result = BulkCreateResult(
            status="dry-run",
            damaged_product_id=None,
            damaged_handle=auto_damaged_handle,
            variants=[],
            messages=["Dry run: no product created."],
        )

        try:
            await log_creation_event(data, result)
        except Exception as e:
            logger.warning(f"[CreationLog] Failed to write dry-run log: {e}")

        return result

    # -------------------------------------------------------
    # Step 4: Create damaged product via create_damaged_pair()
    # DBS NEVER creates canonical products.
    # We pass variant seeds so create_damaged_pair can apply price overrides.
    # -------------------------------------------------------
    created = await create_damaged_pair(
        canonical_handle=data.canonical_handle,
        variants=data.variants,
    )

    damaged = created.get("damaged", {}) or {}
    damaged_id = damaged.get("id")
    damaged_handle = auto_damaged_handle

    # -------------------------------------------------------
    # Step 5: Extract CreatedVariantInfo[]
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

        variant_id_raw = v.get("id")

        extracted_variants.append(
            CreatedVariantInfo(
                condition=title,
                variant_id=str(variant_id_raw) if variant_id_raw is not None else None,
                quantity_set=qty,
                price=float(v.get("price") or 0),
                sku=v.get("sku"),
                barcode=v.get("barcode"),
                inventory_management=v.get("inventory_management"),
                inventory_policy=v.get("inventory_policy"),
            )
        )

    # -------------------------------------------------------
    # Step 6: Build final BulkCreateResult
    # -------------------------------------------------------
    result = BulkCreateResult(
        status="created",
        damaged_product_id=str(damaged_id) if damaged_id is not None else None,
        variants=extracted_variants,
        messages=["Damaged product created successfully."],
    )

    # -------------------------------------------------------
    # Step 7: Initial inventory sync (best-effort)
    # -------------------------------------------------------
    try:
        await _apply_initial_inventory(result)
    except Exception as e:
        logger.warning("[InventoryUpdate] Unexpected error during initial sync: %s", e)

    # -------------------------------------------------------
    # Step 8: Write to creation_log (Option A: log all runs)
    # -------------------------------------------------------
    try:
        await log_creation_event(data, result)
    except Exception as e:
        logger.warning(f"[CreationLog] Failed to write create log: {e}")

    return result

async def _apply_initial_inventory(result: BulkCreateResult) -> None:
    """
    Best-effort initial inventory sync for newly created damaged variants.

    Rules:
      - Only runs when SHOPIFY_LOCATION_ID is configured.
      - For each CreatedVariantInfo with quantity_set > 0:
          1. Fetch variant to get inventory_item_id.
          2. POST inventory_levels/set.json with { location_id, inventory_item_id, available }.
      - Errors are logged and swallowed; never raise.
    """
    if SHOPIFY_LOCATION_ID is None:
        logger.warning(
            "[InventoryUpdate] SHOPIFY_LOCATION_ID not configured; "
            "skipping damaged inventory initialization."
        )
        return

    location_id = SHOPIFY_LOCATION_ID

    for v in result.variants or []:
        try:
            qty = v.quantity_set or 0
            if qty <= 0:
                continue

            variant_id = v.variant_id
            if not variant_id:
                logger.warning("[InventoryUpdate] Missing variant_id in CreatedVariantInfo; skipping.")
                continue

            # 1) Fetch variant to obtain inventory_item_id
            resp = await shopify_client.get(f"variants/{variant_id}.json")
            variant_obj = resp.get("body", {}).get("variant") or {}
            inventory_item_id = variant_obj.get("inventory_item_id")

            if not inventory_item_id:
                logger.warning(
                    "[InventoryUpdate] No inventory_item_id for variant %s; skipping.",
                    variant_id,
                )
                continue

            # 2) Call inventory_levels/set.json to set available quantity at this location
            payload = {
                "location_id": location_id,
                "inventory_item_id": int(inventory_item_id),
                "available": int(qty),
            }

            inv_resp = await shopify_client.post("inventory_levels/set.json", data=payload)
            status = inv_resp.get("status")
            if status and status >= 400:
                logger.warning(
                    "[InventoryUpdate] Shopify responded with status=%s for variant %s payload=%s",
                    status,
                    variant_id,
                    payload,
                )
            else:
                logger.info(
                    "[InventoryUpdate] Set damaged variant %s inventory to %s at location %s",
                    variant_id,
                    qty,
                    location_id,
                )

        except Exception as e:
            logger.warning(
                "[InventoryUpdate] Failed to set inventory for variant_id=%s: %s",
                getattr(v, "variant_id", None),
                e,
            )

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
    canonical_handle: str,
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

    # === AUTO-DERIVED DAMAGED TITLE & HANDLE (Option 1) ===
    # Trim canonical_title at first punctuation token and append ": Damaged"
    # 1. Fetch canonical product from Shopify
    canonical = await find_existing_by_handle(canonical_handle)
    if not canonical:
        raise RuntimeError(f"Canonical product not found for handle '{canonical_handle}'.")

    # 2. Extract canonical title from Shopify
    canonical_title = (canonical.get("title") or "").strip()

    # 3. Derive damaged title by trimming canonical title at first punctuation
    import re
    m = re.split(r"[:;–—-]", canonical_title, maxsplit=1)
    base_title = m[0].strip()
    auto_damaged_title = f"{base_title}: Damaged"

    # 4. Derive damaged handle
    auto_damaged_handle = f"{canonical_handle.strip().lower()}-damaged"

    # Then log
    logger.info(f"[CreateDamagedPair] canonical={canonical_handle}, damaged={auto_damaged_handle}")

    # 5. Extract canonical fields (vendor, product_type, variants, images…)
    canonical_variant = (canonical.get("variants") or [{}])[0]
    canonical_price_raw = canonical_variant.get("price") or "0.00"
    canonical_sku = canonical_variant.get("sku")
    vendor = canonical.get("vendor")
    product_type = canonical.get("product_type")

    # Shopify REST returns tags as a comma-separated string.
    raw_tags = canonical.get("tags") or ""
    if isinstance(raw_tags, str):
        canonical_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    elif isinstance(raw_tags, list):
        # Just in case, be defensive if we ever see a list.
        canonical_tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    else:
        canonical_tags = []

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
            "title": auto_damaged_title,
            "handle": auto_damaged_handle,
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
    # Normalise ID to string to prevent pydantic failures downstream
    if damaged and "id" in damaged:
        damaged["id"] = str(damaged["id"])
    if not damaged:
        raise RuntimeError(f"Failed to create damaged product {auto_damaged_handle}")

    logger.info(f"[CreateDamagedPair] Created damaged id={damaged.get('id')} handle={auto_damaged_handle}")

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