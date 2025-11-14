# services/product_service.py

import logging
from datetime import datetime
from services.shopify_client import shopify_client

logger = logging.getLogger(__name__)

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


async def prevent_duplicate_creation(canonical_handle: str, damaged_handle: str, title: str) -> dict | None:
    """
    Duplicate-prevention logic for damaged book creation.
    - Prevent duplicate damaged products for same canonical
    - Prevent handle collisions
    - Return existing product if duplicate found
    """
    try:
        # 1. Test canonical → damaged uniqueness rule
        existing = await find_existing_by_handle(damaged_handle)
        if existing:
            logger.info(f"[DuplicateCheck] Damaged product already exists for {canonical_handle}: {existing.get('id')}")
            return {"duplicate": True, "existing": existing}

        # 2. Ensure no other damaged product under same canonical root
        root = canonical_handle.lower().strip()
        resp = await shopify_client.get("products.json", query={"handle": f"{root}-damaged"})
        existing_root = resp.get("body", {}).get("products", [])
        if existing_root:
            logger.info(f"[DuplicateCheck] Canonical already has a damaged product: {existing_root[0].get('id')}")
            return {"duplicate": True, "existing": existing_root[0]}

        return {"duplicate": False}
    except Exception as e:
        logger.warning(f"[DuplicateCheck] prevention failed: {e}")
        return {"duplicate": False}


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

# Wrapper used by DBS when creating damaged products.
# Performs duplicate‑prevention before allowing Shopify creation.
async def create_damaged_product_with_duplicate_check(canonical_handle: str, damaged_handle: str, title: str, payload: dict):
    """
    Wrapper used by DBS when creating damaged products.
    Performs duplicate‑prevention before allowing Shopify creation.
    Returns:
        {"duplicate": True, "existing": <product>}  if creation should be skipped
        {"duplicate": False, "created": <product>}  if a new product was made
    """
    # 1. Run duplicate‑prevention preflight
    dup = await prevent_duplicate_creation(canonical_handle, damaged_handle, title)
    if dup and dup.get("duplicate"):
        return dup

    # 2. Safe product creation
    try:
        resp = await shopify_client.post("products.json", data={"product": payload})
        created = resp.get("body", {}).get("product", {})
        return {"duplicate": False, "created": created}
    except Exception as e:
        logger.error(f"[CreateDamaged] Failed creating {damaged_handle}: {e}")
        raise

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
) -> dict:
    """
    Create a canonical product AND its damaged companion at the same time.
    Used by the Admin Dashboard bulk creation wizard.

    Rules:
      • Canonical product:
          - published_at = None  (DRAFT)
          - one default variant
          - SKU = optional ISBN/author (we keep SKU flexible)
          - barcode = ISBN if provided
          - inventory tracking off by default (left to DBS)
      • Damaged product:
          - published_at = now (ACTIVE)
          - three variants:
              Light Damage / Moderate Damage / Heavy Damage
          - barcode always None for damaged variants
      • No inventory adjustments here.
      • No redirects.
      • No canonical metafield update.
      • No DBS upsert — inventory-level events will later do that.

    Returns:
      {
        "canonical": <created canonical product dict>,
        "damaged": <created damaged product dict>
      }
    """

    logger.info(
        "[CreateDamagedPair] Creating canonical=%s damaged=%s",
        canonical_handle, damaged_handle
    )

    # --------------------------------------------------------
    # 1. Prepare canonical product payload
    # --------------------------------------------------------
    canonical_payload = {
        "product": {
            "title": canonical_title,
            "handle": canonical_handle,
            "published_at": None,     # <= DRAFT
            "status": "draft",
            "variants": [
                {
                    "title": canonical_title,
                    "sku": isbn or "",
                    "barcode": isbn or barcode or None,
                    "inventory_management": None,
                    "price": "0.00"
                }
            ],
        }
    }

    # --------------------------------------------------------
    # 2. Prepare damaged product payload
    # --------------------------------------------------------
    damaged_payload = {
        "product": {
            "title": damaged_title,
            "handle": damaged_handle,
            "published_at": datetime.utcnow().isoformat(),  # <= ACTIVE
            "status": "active",
            "variants": [
                {
                    "title": "Light Damage",
                    "sku": "",
                    "barcode": None,
                    "option1": "Light Damage",
                    "price": "0.00"
                },
                {
                    "title": "Moderate Damage",
                    "sku": "",
                    "barcode": None,
                    "option1": "Moderate Damage",
                    "price": "0.00"
                },
                {
                    "title": "Heavy Damage",
                    "sku": "",
                    "barcode": None,
                    "option1": "Heavy Damage",
                    "price": "0.00"
                },
            ],
            "options": [
                {
                    "name": "Condition",
                    "values": ["Light Damage", "Moderate Damage", "Heavy Damage"]
                }
            ],
        }
    }

    # --------------------------------------------------------
    # 3. Create canonical first
    # --------------------------------------------------------
    try:
        resp_canon = await shopify_client.post("products.json", data=canonical_payload)
        canonical = resp_canon.get("body", {}).get("product")
        if not canonical:
            raise RuntimeError(f"Canonical creation failed for {canonical_handle}: {resp_canon}")
    except Exception as e:
        logger.error(f"[CreateDamagedPair] Canonical creation error for {canonical_handle}: {e}")
        raise

    canonical_id = canonical.get("id")
    logger.info(f"[CreateDamagedPair] Canonical created id={canonical_id}")

    # --------------------------------------------------------
    # 4. Create damaged next
    # --------------------------------------------------------
    try:
        resp_dmg = await shopify_client.post("products.json", data=damaged_payload)
        damaged = resp_dmg.get("body", {}).get("product")
        if not damaged:
            raise RuntimeError(f"Damaged creation failed for {damaged_handle}: {resp_dmg}")
    except Exception as e:
        logger.error(f"[CreateDamagedPair] Damaged creation error for {damaged_handle}: {e}")
        raise

    damaged_id = damaged.get("id")
    logger.info(f"[CreateDamagedPair] Damaged created id={damaged_id}")

    # --------------------------------------------------------
    # 5. Return structured result
    # --------------------------------------------------------
    return {
        "canonical": canonical,
        "damaged": damaged,
    }

# --------------------------------------------------------
# _publish_to_online_store and _unpublish_from_online_store are not referenced in routes.py or used_book_manager.py (both use set_product_publish_status).
# Decision point: keep them for future fallback OR remove if we want to enforce one publishing pathway.
# --------------------------------------------------------

async def _publish_to_online_store(product_id: str) -> None:
    """
    Ensure the product is listed in Online Store.
    POST /product_listings.json
    """
    try:
        payload = {"product_listing": {"product_id": product_id}}
        await shopify_client.post("product_listings.json", data=payload)
    except Exception as e:
        logger.warning(f"Online Store publish failed for product {product_id}: {str(e)}")


async def _unpublish_from_online_store(product_id: str) -> None:
    """
    Ensure the product is removed from Online Store listing.
    DELETE /product_listings/{id}.json
    """
    try:
        await shopify_client.delete(f"product_listings/{product_id}.json")
    except Exception as e:
        # If it wasn't listed, Shopify may 404—treat as benign
        logger.info(f"Online Store unpublish note for product {product_id}: {str(e)}")

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