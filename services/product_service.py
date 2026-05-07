# services/product_service.py

import logging
import os
from collections import defaultdict
from datetime import datetime
from services.shopify_client import shopify_client
from services.supabase_client import get_client
from backend.app.schemas import (
    BulkCreateRequest,
    BulkCreateResult,
    VariantSeed,
    CreatedVariantInfo,
    InventorySeed,
    BulkCreateInput,
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

# Manual collection that all in-stock damaged products should belong to.
DAMAGED_BOOKS_COLLECTION_GID = "gid://shopify/Collection/279535911045"

# Shopify Standard Product Taxonomy — Media > Books > Print Books.
# Set via productUpdate.category (NOT via collectionAddProducts).
PRINT_BOOKS_TAXONOMY_GID = "gid://shopify/TaxonomyCategory/me-1-3"

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

# --------------------------------------------------------
# Bulk input resolver for canonical products
# --------------------------------------------------------

# ---------------------------------------------------------------------------
# GraphQL queries used exclusively by resolve_bulk_inputs.
# Explicit, self-contained queries so we own the selection set and can
# guarantee price, weight, and all other required fields are present.
# ---------------------------------------------------------------------------

# Fetch a full product by GID — used for barcode and direct product-ID paths.
# Only requests fields actually needed by the service layer.
# Note: weight/weightUnit moved from ProductVariant to
# inventoryItem.measurement.weight in Shopify Admin API 2025-01.
_CANONICAL_PRODUCT_QUERY = """
query GetCanonicalProduct($id: ID!) {
  product(id: $id) {
    id
    handle
    title
    vendor
    productType
    tags
    images(first: 1) {
      edges {
        node { url }
      }
    }
    variants(first: 3) {
      edges {
        node {
          id
          price
          sku
          barcode
          inventoryItem {
            measurement {
              weight {
                value
                unit
              }
            }
          }
        }
      }
    }
  }
}
"""

# Fetch a product variant by GID and return its parent product.
# Used when the user enters a Shopify variant ID directly.
_VARIANT_TO_PRODUCT_QUERY = """
query GetVariantParentProduct($id: ID!) {
  productVariant(id: $id) {
    product {
      id
      handle
      title
      vendor
      productType
      tags
      images(first: 1) {
        edges {
          node { url }
        }
      }
      variants(first: 3) {
        edges {
          node {
            id
            price
            sku
            barcode
            inventoryItem {
              measurement {
                weight {
                  value
                  unit
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

# Shopify GQL WeightUnit enum → service-layer unit string
_WEIGHT_UNIT_MAP: dict[str, str] = {
    "GRAMS":      "g",
    "KILOGRAMS":  "kg",
    "OUNCES":     "oz",
    "POUNDS":     "lb",
}


def _normalize_gql_product(gql_product: dict) -> dict:
    """
    Normalize a Shopify Admin GraphQL product node into a flat shape
    compatible with the rest of the service layer.

    Key differences from REST:
      - GQL tags     → list of strings;       normalized → comma-separated string
      - GQL variants → edges/node connection; normalized → flat list
      - GQL weightUnit → GRAMS/KILOGRAMS;     normalized → g/kg
      - GQL images   → edges/node connection; normalized → [{src: url}]
    """
    if not gql_product:
        return {}

    # ── Tags ──────────────────────────────────────────────────────────────
    raw_tags = gql_product.get("tags") or []
    tags_str = ", ".join(raw_tags) if isinstance(raw_tags, list) else str(raw_tags)

    # ── Images ────────────────────────────────────────────────────────────
    images: list[dict] = []
    for edge in ((gql_product.get("images") or {}).get("edges") or []):
        url = (edge.get("node") or {}).get("url")
        if url:
            images.append({"src": url})

    # ── Variants ──────────────────────────────────────────────────────────
    variants: list[dict] = []
    for edge in ((gql_product.get("variants") or {}).get("edges") or []):
        node = edge.get("node") or {}

        # Weight moved from ProductVariant.weight/weightUnit to
        # ProductVariant.inventoryItem.measurement.weight in API 2025-01
        weight_node = (
            (node.get("inventoryItem") or {})
            .get("measurement") or {}
        ).get("weight") or {}
        weight_value    = weight_node.get("value")
        weight_unit_raw = (weight_node.get("unit") or "").upper()

        variants.append({
            "id":                   str(node.get("id") or "").split("/")[-1],
            "price":                node.get("price"),
            "compare_at_price":     node.get("compareAtPrice"),
            "sku":                  node.get("sku"),
            "barcode":              node.get("barcode"),
            "weight":               weight_value,
            "weight_unit":          _WEIGHT_UNIT_MAP.get(weight_unit_raw, weight_unit_raw.lower() or "g"),
            "inventory_management": (node.get("inventoryManagement") or "shopify").lower(),
            "inventory_policy":     (node.get("inventoryPolicy") or "deny").lower(),
        })

    return {
        "id":           str(gql_product.get("id") or "").split("/")[-1],
        "handle":       gql_product.get("handle") or "",
        "title":        gql_product.get("title") or "",
        "vendor":       gql_product.get("vendor"),
        "product_type": gql_product.get("productType"),
        "tags":         tags_str,
        "images":       images,
        "variants":     variants,
    }


async def _fetch_product_by_gid(product_gid: str) -> dict | None:
    """
    Fetch and normalize a canonical product by its GID.
    Returns None if the product is not found or GQL returns no data.

    IMPORTANT: Always logs GQL-level errors (HTTP 200 with errors[] in body)
    so that misconfigured queries or permission issues surface in logs rather
    than silently returning None.
    """
    resp = await shopify_client.graphql(_CANONICAL_PRODUCT_QUERY, {"id": product_gid})
    body = resp.get("body", resp)

    # Surface GQL errors — these return HTTP 200 but have an errors[] key
    gql_errors = body.get("errors") or []
    if gql_errors:
        logger.warning(
            "[BulkResolve] GQL errors fetching product %s: %s",
            product_gid, gql_errors,
        )

    gql_product = (body.get("data") or {}).get("product") or {}
    if not gql_product:
        logger.warning(
            "[BulkResolve] GQL returned no product data for %s "
            "(errors=%s, data=%s)",
            product_gid,
            gql_errors or "none",
            body.get("data"),
        )
        return None
    return _normalize_gql_product(gql_product)


async def resolve_bulk_inputs(inputs: list[BulkCreateInput]) -> list[dict]:
    """
    Resolve BulkCreateInput[] → canonical Shopify products.

    Rules enforced:
      - Each input must resolve to exactly one canonical product
      - Canonical product must exist
      - Canonical product must have exactly one variant
      - Damaged / used products are invalid as inputs

    Resolution cascade (tried in order until one succeeds):

      1. Barcode / ISBN lookup — handles all barcode formats:
           - ISBN-10 (10 digits)
           - ISBN-13 (13 digits starting with 978/979)
           - Internal / alphanumeric barcodes (e.g. DISFRUTARVOL2SP)
           - Any other barcode stored on a Shopify variant

      2. Shopify variant ID → parent product — for numeric values that are
           variant IDs, not barcodes (e.g. 6846980325509).

      3. Shopify product ID → direct product lookup — for numeric values
           that are product IDs.

    The `type` hint from the frontend is no longer used for routing; the
    cascade tries all strategies automatically.
    """

    resolved: list[dict] = []
    seen_product_ids: set[str] = set()

    for inp in inputs:
        value = (inp.value or "").strip()
        if not value:
            continue

        logger.info("[BulkResolve] Resolving '%s'", value)

        product: dict | None = None
        strategies_tried: list[str] = []

        # ── Strategy 1: barcode / ISBN lookup ─────────────────────────────
        # Works for ISBN-10, ISBN-13, and any custom alphanumeric barcode.
        try:
            resolution = await shopify_client.resolve_product_by_barcode_gql(value)
            if resolution:
                product_id = resolution.get("product_id") or ""
                # Strip GID prefix if resolve_product_by_barcode_gql returns
                # a full GID string (e.g. "gid://shopify/Product/123") rather
                # than a plain numeric ID.
                raw_pid = str(product_id).split("/")[-1]
                if raw_pid:
                    product_gid = f"gid://shopify/Product/{raw_pid}"
                    product = await _fetch_product_by_gid(product_gid)
                    if product:
                        logger.info("[BulkResolve] Resolved '%s' via barcode lookup", value)
        except Exception as e:
            logger.debug("[BulkResolve] Barcode strategy failed for '%s': %s", value, e)
        strategies_tried.append("barcode")

        # ── Strategy 2: Shopify variant ID → parent product ───────────────
        # Numeric values only. A 13-digit number like 6846980325509 that is
        # a variant ID (not a barcode) will reach here after strategy 1 fails.
        if not product and value.isdigit():
            try:
                variant_gid = f"gid://shopify/ProductVariant/{value}"
                resp = await shopify_client.graphql(
                    _VARIANT_TO_PRODUCT_QUERY, {"id": variant_gid}
                )
                body = resp.get("body", resp)
                gql_variant = (body.get("data") or {}).get("productVariant") or {}
                gql_product  = gql_variant.get("product") or {}
                if gql_product:
                    product = _normalize_gql_product(gql_product)
                    if product:
                        logger.info("[BulkResolve] Resolved '%s' via variant ID → product", value)
            except Exception as e:
                logger.debug("[BulkResolve] Variant ID strategy failed for '%s': %s", value, e)
            strategies_tried.append("variant_id")

        # ── Strategy 3: Shopify product ID → direct product lookup ─────────
        if not product and value.isdigit():
            try:
                product_gid = f"gid://shopify/Product/{value}"
                product = await _fetch_product_by_gid(product_gid)
                if product:
                    logger.info("[BulkResolve] Resolved '%s' via product ID", value)
            except Exception as e:
                logger.debug("[BulkResolve] Product ID strategy failed for '%s': %s", value, e)
            strategies_tried.append("product_id")

        if not product:
            raise ValueError(
                f"Could not resolve '{value}' after trying: {', '.join(strategies_tried)}. "
                "Check that the barcode, variant ID, or product ID is correct."
            )

        handle = (product.get("handle") or "").lower()

        # ── Handle sanitization ───────────────────────────────────────────────
        # Shopify automatically prefixes handles with "copy-of-" when a product
        # is duplicated in the admin. This prefix must be stripped before use:
        # it would otherwise propagate into the damaged handle
        # (e.g. "copy-of-foo-damaged" instead of "foo-damaged").
        # Log a warning so the admin knows the canonical product handle needs
        # correcting in Shopify.
        if handle.startswith("copy-of-"):
            original_handle = handle
            handle = handle[len("copy-of-"):]
            logger.warning(
                "[BulkResolve] Stripped 'copy-of-' prefix: '%s' → '%s'. "
                "The canonical product handle should be corrected in Shopify Admin "
                "to avoid this sanitization being required.",
                original_handle,
                handle,
            )

        raw_tags = product.get("tags") or ""
        tags = [t.strip().lower() for t in raw_tags.split(",") if t.strip()]

        if handle.endswith("-damaged") or "damaged" in tags:
            raise ValueError(
                f"Damaged product cannot be used as canonical input: {handle}"
            )

        variants = product.get("variants") or []

        if len(variants) != 1:
            raise ValueError(
                f"Canonical product '{handle}' has {len(variants)} variants; exactly 1 required"
            )

        pid = str(product.get("id"))
        if pid in seen_product_ids:
            raise ValueError(
                f"Duplicate canonical product resolved multiple times: product_id={pid}"
            )
        seen_product_ids.add(pid)

        logger.info(
            "[BulkResolve] Canonical resolved: product_id=%s handle=%s title=%s price=%s weight=%s",
            product.get("id"),
            handle,
            product.get("title"),
            (variants[0] or {}).get("price"),
            (variants[0] or {}).get("weight"),
        )

        resolved.append(
            {
                "product_id": str(product.get("id")),
                "handle": handle,
                "title": product.get("title"),
                "variant": variants[0],
            }
        )

    return resolved

def _snake_handle(handle: str) -> str:
    h = (handle or "").strip().lower()
    return h.replace("-", "_").replace(" ", "_")

def _make_barcode_for_condition(canonical_handle: str, condition_key: str) -> str:
    base = _snake_handle(canonical_handle)
    return f"{base}_{condition_key}"


# ---------------------------------------------------------------------------
# Pure preview helper
# ---------------------------------------------------------------------------

def compute_damaged_variant_preview(
    canonical_product_id: str,
    canonical_handle: str,
    canonical_variant: dict,
    inventory_seed: InventorySeed,
    existing_inventory: dict | None = None,
) -> list[dict]:
    """
    Pure function: derive damaged variant preview rows.
    NO Shopify calls. NO side effects.

    existing_inventory: result of fetch_existing_damaged_inventory(), or None
    for fresh creates. When provided, each row includes:
      - action:       "update" (product exists) vs "create" (new)
      - existing_qty: current stock for that condition
      - new_total:    existing_qty + inventory_seed (what will result after confirm)
    """

    canonical_price = float(canonical_variant.get("price") or 0)
    canonical_sku = canonical_variant.get("sku")
    is_update = existing_inventory is not None
    by_cond = (existing_inventory or {}).get("by_condition", {})

    rows = []

    for cond_key, meta in CONDITION_META.items():
        pct = meta["default_discount"]
        price = f"{canonical_price * (1 - pct):.2f}"
        seed_qty = getattr(inventory_seed, cond_key, 0)
        existing_qty = by_cond.get(cond_key, {}).get("qty", 0) if is_update else 0

        rows.append({
            "canonical_product_id": canonical_product_id,
            "canonical_handle":     canonical_handle,
            "condition":            cond_key,
            "title":                meta["title"],
            "price":                price,
            "discount_pct":         pct,
            "inventory_seed":       seed_qty,
            "sku":                  canonical_sku,
            "barcode":              _make_barcode_for_condition(canonical_handle, cond_key),
            # Enrichment fields — populated when damaged product already exists
            "action":               "update" if is_update else "create",
            "existing_qty":         existing_qty,
            "new_total":            existing_qty + seed_qty,
        })

    return rows


# ---------------------------------------------------------------------------
# Existing damaged inventory fetch (for preview enrichment)
# ---------------------------------------------------------------------------

_DAMAGED_INVENTORY_QUERY = """
query GetDamagedInventory($handle: String!) {
  productByHandle(handle: $handle) {
    id
    status
    variants(first: 5) {
      edges {
        node {
          id
          title
          inventoryQuantity
          inventoryItem { id }
        }
      }
    }
  }
}
"""


async def fetch_existing_damaged_inventory(damaged_handle: str) -> dict | None:
    """
    If a damaged product exists for the given handle, return:
      {
        "product_id": str,
        "status": str,                       # "active" | "draft" | "archived"
        "by_condition": {
          "light":    {"qty": int, "variant_id": str, "inventory_item_id": str},
          "moderate": {...},
          "heavy":    {...},
        }
      }
    Returns None if the product does not exist.
    """
    try:
        resp = await shopify_client.graphql(_DAMAGED_INVENTORY_QUERY, {"handle": damaged_handle})
        body = resp.get("body", resp)
        product = (body.get("data") or {}).get("productByHandle") or {}
        if not product:
            return None

        by_condition: dict[str, dict] = {}
        for edge in ((product.get("variants") or {}).get("edges") or []):
            node = edge.get("node") or {}
            cond = _normalize_condition_from_title(node.get("title"))
            if not cond:
                continue
            inv_item_id = ((node.get("inventoryItem") or {}).get("id") or "").split("/")[-1]
            by_condition[cond] = {
                "qty":               int(node.get("inventoryQuantity") or 0),
                "variant_id":        str(node.get("id") or "").split("/")[-1],
                "inventory_item_id": inv_item_id,
            }

        return {
            "product_id": str(product.get("id") or "").split("/")[-1],
            "status":     (product.get("status") or "").lower(),
            "by_condition": by_condition,
        }
    except Exception as e:
        logger.warning("[PreviewEnrich] Failed to fetch existing damaged inventory for '%s': %s", damaged_handle, e)
        return None

async def _add_to_damaged_collection(product_id: str) -> None:
    """
    Add a product to the damaged-books manual collection via GraphQL.
    Errors are logged and swallowed — never blocks the main flow.
    """
    raw_id = str(product_id).split("/")[-1]
    product_gid = f"gid://shopify/Product/{raw_id}"

    mutation = """
    mutation AddToCollection($collectionId: ID!, $productIds: [ID!]!) {
      collectionAddProducts(id: $collectionId, productIds: $productIds) {
        collection { id title }
        userErrors { field message }
      }
    }
    """
    variables = {
        "collectionId": DAMAGED_BOOKS_COLLECTION_GID,
        "productIds": [product_gid],
    }

    try:
        resp = await shopify_client.graphql(mutation, variables)
        body = resp.get("body", resp)
        errors = (
            (body.get("data") or {})
            .get("collectionAddProducts", {})
            .get("userErrors") or []
        )
        if errors:
            logger.warning(
                "[Collection] Errors adding product %s to damaged-books: %s",
                product_gid, errors,
            )
        else:
            logger.info(
                "[Collection] Added product %s to damaged-books collection", product_gid
            )
    except Exception as e:
        logger.warning(
            "[Collection] Failed to add product %s to damaged-books collection: %s",
            product_gid, e,
        )


# ---------------------------------------------------------------------------
# Publication helpers
# ---------------------------------------------------------------------------

# Cache stores list of {id, name} dicts — never bare IDs — so logs always
# show which channels are included. None = not yet fetched.
_publication_ids_cache: list[dict] | None = None


async def _get_publications() -> list[dict]:
    """
    Return all Shopify sales channel publications as {id, name} dicts.

    Two-step approach:
      1. Query `publications(first: 20)` — returns app-accessible channels
      2. Query `channel(handle: "online-store")` as an explicit fallback,
         because the Online Store is a built-in channel that may not appear
         in the publications list depending on app scopes/configuration.

    Result is cached at module level for the process lifetime.
    """
    global _publication_ids_cache
    if _publication_ids_cache is not None:
        return _publication_ids_cache

    pubs: list[dict] = []

    # ── Step 1: all app-accessible publications ───────────────────────────
    pubs_query = """
    query GetPublications {
      publications(first: 20) {
        edges {
          node { id name }
        }
      }
    }
    """
    try:
        resp = await shopify_client.graphql(pubs_query, {})
        body = resp.get("body", resp)
        edges = (body.get("data") or {}).get("publications", {}).get("edges") or []
        for edge in edges:
            node = edge.get("node") or {}
            if node.get("id") and node.get("name"):
                pubs.append({"id": node["id"], "name": node["name"]})
    except Exception as e:
        logger.warning("[Publish] publications query failed: %s", e)

    # ── Step 2: explicit Online Store fallback ────────────────────────────
    # The Online Store is a built-in Shopify channel. It may not appear in
    # the publications query if the app's access scopes don't surface it.
    # Query it directly by handle to ensure it's always included.
    known_ids = {p["id"] for p in pubs}
    online_store_query = """
    query GetOnlineStorePublication {
      publication(id: null) { id }
      channels(first: 5) {
        edges {
          node {
            id
            name
            handle
            publication { id }
          }
        }
      }
    }
    """
    # Cleaner approach: publications can also be found via channel handle
    online_store_pub_query = """
    query GetChannels {
      channels(first: 10) {
        edges {
          node {
            name
            handle
            publication { id }
          }
        }
      }
    }
    """
    try:
        resp = await shopify_client.graphql(online_store_pub_query, {})
        body = resp.get("body", resp)
        ch_edges = (body.get("data") or {}).get("channels", {}).get("edges") or []
        for edge in ch_edges:
            node = edge.get("node") or {}
            pub  = node.get("publication") or {}
            pub_id   = pub.get("id")
            pub_name = node.get("name") or node.get("handle") or "unknown"
            if pub_id and pub_id not in known_ids:
                pubs.append({"id": pub_id, "name": pub_name})
                known_ids.add(pub_id)
                logger.info("[Publish] Added channel '%s' via channels query: %s", pub_name, pub_id)
    except Exception as e:
        logger.warning("[Publish] channels query failed: %s", e)

    _publication_ids_cache = pubs
    logger.info(
        "[Publish] Cached %d publication(s): %s",
        len(pubs),
        [(p["name"], p["id"]) for p in pubs],
    )
    return pubs


async def _publish_product(product_id: str) -> None:
    """
    Publish a damaged product to all sales channels.

    Approach:
      1. productUpdate → status: ACTIVE  (marks product non-draft in Shopify)
      2. publishablePublish with every publication ID from _get_publications()
         — covers Online Store, POS, and any other connected channels.

    All calls are best-effort: errors are logged and swallowed so they
    never block the main creation flow.
    """
    raw_id = str(product_id).split("/")[-1]
    product_gid = f"gid://shopify/Product/{raw_id}"

    # ── Step 1: set status ACTIVE ─────────────────────────────────────────
    activate_mutation = """
    mutation ActivateProduct($input: ProductInput!) {
      productUpdate(input: $input) {
        product { id status }
        userErrors { field message }
      }
    }
    """
    try:
        resp = await shopify_client.graphql(activate_mutation, {
            "input": {"id": product_gid, "status": "ACTIVE"},
        })
        body = resp.get("body", resp)
        errors = (body.get("data") or {}).get("productUpdate", {}).get("userErrors") or []
        if errors:
            logger.warning("[Publish] productUpdate errors for %s: %s", product_gid, errors)
        else:
            status = ((body.get("data") or {}).get("productUpdate", {}).get("product") or {}).get("status")
            logger.info("[Publish] Set status=%s for %s", status, product_gid)
    except Exception as e:
        logger.warning("[Publish] productUpdate (activate) failed for %s: %s", product_gid, e)

    # ── Step 2: publishablePublish to every channel ───────────────────────
    publications = await _get_publications()
    if not publications:
        logger.warning(
            "[Publish] No publications found — product %s will remain unpublished from channels.",
            product_gid,
        )
        return

    logger.info(
        "[Publish] Publishing %s to %d channel(s): %s",
        product_gid,
        len(publications),
        [p["name"] for p in publications],
    )

    publish_mutation = """
    mutation PublishProduct($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        publishable {
          availablePublicationCount
          publicationCount
        }
        userErrors { field message }
      }
    }
    """
    pub_input = [{"publicationId": p["id"]} for p in publications]
    try:
        resp = await shopify_client.graphql(publish_mutation, {
            "id": product_gid,
            "input": pub_input,
        })
        body = resp.get("body", resp)
        result  = (body.get("data") or {}).get("publishablePublish") or {}
        errors  = result.get("userErrors") or []
        pub_data = result.get("publishable") or {}

        if errors:
            logger.warning("[Publish] publishablePublish userErrors for %s: %s", product_gid, errors)

        pub_count   = pub_data.get("publicationCount",          "?")
        avail_count = pub_data.get("availablePublicationCount", "?")

        if pub_count != avail_count:
            logger.warning(
                "[Publish] Partial publish for %s: %s/%s channels published. "
                "Check app scopes — some channels may require additional permissions.",
                product_gid, pub_count, avail_count,
            )
        else:
            logger.info(
                "[Publish] Successfully published %s to %s/%s channel(s).",
                product_gid, pub_count, avail_count,
            )
    except Exception as e:
        logger.warning("[Publish] publishablePublish failed for %s: %s", product_gid, e)


async def _set_product_category(product_id: str) -> None:
    """
    Assign the Shopify Standard Product Taxonomy category 'Print Books'
    (Media > Books > Print Books, gid://shopify/TaxonomyCategory/me-1-3)
    to a product via productUpdate.

    This is NOT a collection — it sets the productCategory field on the product
    itself. It applies across all channels and Shopify taxonomy reporting.
    Idempotent: safe to call on both fresh creates and existing products.
    """
    raw_id = str(product_id).split("/")[-1]
    product_gid = f"gid://shopify/Product/{raw_id}"

    mutation = """
    mutation SetProductCategory($input: ProductInput!) {
      productUpdate(input: $input) {
        product {
          id
          category { id name fullName }
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
            logger.warning("[Taxonomy] Errors setting Print Books category on %s: %s", product_gid, errors)
        else:
            category = (
                (body.get("data") or {})
                .get("productUpdate", {})
                .get("product", {})
                .get("category") or {}
            )
            logger.info(
                "[Taxonomy] Set category '%s' on product %s",
                category.get("fullName", PRINT_BOOKS_TAXONOMY_GID),
                product_gid,
            )
    except Exception as e:
        logger.warning("[Taxonomy] Failed to set category on %s: %s", product_gid, e)


# ---------------------------------------------------------------------------
# Inventory update for already-existing damaged products
# ---------------------------------------------------------------------------

_ADJUST_INVENTORY_MUTATION = """
mutation AdjustInventory($input: InventoryAdjustQuantitiesInput!) {
  inventoryAdjustQuantities(input: $input) {
    userErrors { field message }
    inventoryAdjustmentGroup {
      changes {
        name
        delta
        quantityAfterChange
        item { id }
      }
    }
  }
}
"""


async def _update_existing_damaged_inventory(
    canonical_handle: str,
    inventory: dict,  # {"light": int, "moderate": int, "heavy": int}
) -> BulkCreateResult:
    """
    Add inventory quantities to an already-existing damaged product.

    Uses inventoryAdjustQuantities (GQL delta mutation) — ADDS the requested
    quantities on top of whatever currently exists. Never overwrites.

    Also publishes (if draft) and adds to damaged-books collection when any
    variant ends up in stock.
    """
    if SHOPIFY_LOCATION_ID is None:
        raise RuntimeError(
            "SHOPIFY_LOCATION_ID / DBS_SHOPIFY_LOCATION_ID not configured; "
            "cannot update inventory."
        )

    location_gid = f"gid://shopify/Location/{SHOPIFY_LOCATION_ID}"
    damaged_handle = f"{canonical_handle}-damaged"

    # Fetch current damaged product with inventory item IDs via GQL
    existing = await fetch_existing_damaged_inventory(damaged_handle)
    if not existing:
        raise RuntimeError(f"Damaged product not found for handle '{damaged_handle}'")

    damaged_id     = existing["product_id"]
    by_condition   = existing["by_condition"]
    current_status = existing["status"]

    # Build delta changes — one per condition where delta > 0
    changes: list[dict] = []
    result_variants: list[CreatedVariantInfo] = []

    for cond_key, delta in inventory.items():
        delta = int(delta or 0)
        cond_data = by_condition.get(cond_key) or {}
        inventory_item_id = cond_data.get("inventory_item_id")
        current_qty = cond_data.get("qty", 0)
        variant_id  = cond_data.get("variant_id")

        if not inventory_item_id:
            logger.warning(
                "[UpdateInventory] No inventory_item_id for condition=%s handle=%s — skipping",
                cond_key, damaged_handle,
            )
            continue

        if delta > 0:
            changes.append({
                "inventoryItemId": f"gid://shopify/InventoryItem/{inventory_item_id}",
                "locationId":      location_gid,
                "delta":           delta,
            })
            logger.info(
                "[UpdateInventory] Will add %s to condition=%s (currently %s → %s)",
                delta, cond_key, current_qty, current_qty + delta,
            )
        else:
            logger.info(
                "[UpdateInventory] Skipping condition=%s — delta is 0",
                cond_key,
            )

        result_variants.append(
            CreatedVariantInfo(
                condition=cond_key,
                variant_id=variant_id,
                quantity_set=current_qty + delta,
                price=None,
                sku=None,
                barcode=None,
                inventory_management="shopify",
                inventory_policy="deny",
            )
        )

    # Execute the delta mutation
    if changes:
        try:
            resp = await shopify_client.graphql(
                _ADJUST_INVENTORY_MUTATION,
                {
                    "input": {
                        "reason":  "correction",
                        "name":    "available",
                        "changes": changes,
                    }
                },
            )
            body   = resp.get("body", resp)
            result = (body.get("data") or {}).get("inventoryAdjustQuantities") or {}
            errors = result.get("userErrors") or []

            if errors:
                logger.warning("[UpdateInventory] inventoryAdjustQuantities errors: %s", errors)
            else:
                adj_changes = (result.get("inventoryAdjustmentGroup") or {}).get("changes") or []
                for ch in adj_changes:
                    logger.info(
                        "[UpdateInventory] Adjusted inventory_item=%s delta=%s → qty_after=%s",
                        (ch.get("item") or {}).get("id", "?"),
                        ch.get("delta"),
                        ch.get("quantityAfterChange"),
                    )
        except Exception as e:
            logger.warning("[UpdateInventory] inventoryAdjustQuantities failed: %s", e)
    else:
        logger.info("[UpdateInventory] No non-zero deltas — inventory unchanged for %s", damaged_handle)

    # Publish + collection + category (same as fresh create path)
    total_delta = sum(int(v or 0) for v in inventory.values())
    if total_delta > 0:
        try:
            await _add_to_damaged_collection(damaged_id)
        except Exception as e:
            logger.warning("[Collection] Failed to add existing product to collection: %s", e)

        if current_status != "active":
            try:
                await _publish_product(damaged_id)
            except Exception as e:
                logger.warning("[Publish] Failed to publish existing product %s: %s", damaged_id, e)

        try:
            await _set_product_category(damaged_id)
        except Exception as e:
            logger.warning("[Taxonomy] Failed to set category on existing product %s: %s", damaged_id, e)

    return BulkCreateResult(
        status="updated",
        damaged_product_id=damaged_id,
        damaged_handle=damaged_handle,
        variants=result_variants,
        messages=[
            f"Inventory updated: added {sum(int(v or 0) for v in inventory.values())} cop"
            f"{'y' if sum(int(v or 0) for v in inventory.values()) == 1 else 'ies'} across "
            f"{sum(1 for v in inventory.values() if int(v or 0) > 0)} condition(s)."
        ],
    )


# ---------------------------------------------------------------------------
# Confirm-phase writer: create OR update damaged products from preview payloads
# ---------------------------------------------------------------------------

async def create_damaged_from_preview_items(
    items: list,  # List[BulkCreateConfirmItem] — one row per condition
) -> dict:
    """
    Confirm-phase writer.

    Receives a flat list of BulkCreateConfirmItem (one per condition per canonical).
    Groups them by canonical_product_id, reconstructs full inventory dict, then:
      - If damaged product ALREADY EXISTS → update inventory quantities only
      - If NOT → create fresh product with weight inheritance + add to collection
    """

    if not items:
        raise ValueError("Confirm requires non-empty preview items[]")

    # ------------------------------------------------------------------
    # 1. Group items by canonical_product_id → reconstruct inventory dict
    # ------------------------------------------------------------------
    # Each BulkCreateConfirmItem has: canonical_product_id, canonical_handle,
    # condition_key, inventory (int — quantity for that condition).
    grouped: dict[str, dict] = {}  # keyed by canonical_product_id str

    for item in items:
        # Accept both Pydantic model instances and plain dicts
        if hasattr(item, "canonical_product_id"):
            cid = str(item.canonical_product_id)
            handle = item.canonical_handle
            cond = item.condition_key
            qty = int(item.inventory or 0)
        else:
            cid = str(item.get("canonical_product_id", ""))
            handle = item.get("canonical_handle", "")
            cond = item.get("condition_key", "")
            qty = int(item.get("inventory", 0) or 0)

        if not cid or not handle:
            logger.warning("[BulkConfirm] Skipping malformed item: cid=%s handle=%s", cid, handle)
            continue

        if cid not in grouped:
            grouped[cid] = {
                "canonical_handle": handle,
                "inventory": {"light": 0, "moderate": 0, "heavy": 0},
            }

        if cond in ("light", "moderate", "heavy"):
            grouped[cid]["inventory"][cond] = qty

    if not grouped:
        raise ValueError("No valid items after grouping; check canonical_product_id and condition_key fields.")

    # ------------------------------------------------------------------
    # 2. Process each canonical — create fresh or update existing
    # ------------------------------------------------------------------
    all_results: list[dict] = []
    errors: list[dict] = []

    for cid, group in grouped.items():
        canonical_handle: str = group["canonical_handle"]
        inventory: dict = group["inventory"]
        damaged_handle = f"{canonical_handle}-damaged"

        logger.info(
            "[BulkConfirm] Processing canonical_handle=%s inventory=%s",
            canonical_handle, inventory,
        )

        try:
            # Check whether the damaged product already exists
            dup_result = await check_damaged_duplicate(
                canonical_handle=canonical_handle,
                damaged_handle=damaged_handle,
            )
            damaged_exists = dup_result.get("conflicts", {}).get("damaged_exists", False)

            if damaged_exists:
                # UPDATE PATH — product exists, just set quantities
                logger.info("[BulkConfirm] Damaged product exists → updating inventory for %s", damaged_handle)
                result = await _update_existing_damaged_inventory(
                    canonical_handle=canonical_handle,
                    inventory=inventory,
                )
            else:
                # CREATE PATH — new damaged product
                logger.info("[BulkConfirm] Damaged product not found → creating fresh for %s", canonical_handle)
                variants = [
                    VariantSeed(condition=cond, quantity=qty, price_override=None)
                    for cond, qty in inventory.items()
                ]
                data = BulkCreateRequest(
                    canonical_handle=canonical_handle,
                    variants=variants,
                    dry_run=False,
                )
                result = await create_damaged_product_with_duplicate_check(data)

            serialized = result.model_dump() if hasattr(result, "model_dump") else result
            all_results.append(serialized)

        except Exception as e:
            logger.exception("[BulkConfirm] Failed for canonical_handle=%s", canonical_handle)
            errors.append({"canonical_handle": canonical_handle, "error": str(e)})

    return {
        "ok": len(errors) == 0,
        "results": all_results,
        "errors": errors,
        "meta": {
            "processed": len(grouped),
            "succeeded": len(all_results),
            "failed": len(errors),
        },
    }


def _normalize_condition_from_title(title: str | None) -> str | None:
    """
    Map variant title (e.g. 'Light Damage') back to canonical condition keys.
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
    Return first Shopify product that matches the given handle.
    """
    try:
        base = (handle or "").strip().lower()

        suffix_candidates = [base, f"{base}-1", f"{base}-2", f"{base}-3"]
        for h in suffix_candidates:
            resp = await shopify_client.get("products.json", query={"handle": h})
            products = resp.get("body", {}).get("products", [])
            if products:
                return products[0]

        resp = await shopify_client.get("products.json", query={"limit": 250})
        all_products = resp.get("body", {}).get("products", []) or []

        for p in all_products:
            h = (p.get("handle") or "").strip().lower()
            if h == base or h.startswith(f"{base}-"):
                logger.info(
                    "[find_existing_by_handle] Fallback match: requested=%s matched=%s", base, h,
                )
                return p

        logger.info(
            "[find_existing_by_handle] No product found for handle base='%s' after suffix + fallback scan",
            base,
        )
        return None

    except Exception as e:
        logger.warning(
            "[DuplicateCheck] Failed finding existing product by handle=%s: %s", handle, e,
        )
        return None


async def check_damaged_duplicate(
    canonical_handle: str,
    damaged_handle: str | None = None,
) -> dict:
    """
    Duplicate/conflict check.

    Rules:
      - Canonical product MUST exist → if missing → conflict
      - Damaged product (auto handle: canonical + "-damaged") MUST NOT exist
      - Any damaged inventory rows in Supabase → conflict
    """

    # Auto-derive damaged_handle if not provided
    if damaged_handle is None:
        damaged_handle = f"{canonical_handle.strip().lower()}-damaged"

    logger.info(
        "[DuplicateCheck] Checking canonical='%s', damaged='%s'",
        canonical_handle, damaged_handle,
    )

    result = {
        "status": "ok",
        "canonical_handle": canonical_handle,
        "damaged_handle": damaged_handle,
        "conflicts": {
            "canonical_missing": False,
            "damaged_exists": False,
            "inventory_present": False,
        },
        "existing_products": {
            "canonical": None,
            "damaged": None,
        },
        "inventory_rows": [],
        "safe_to_create": True,
    }

    try:
        base_canonical = canonical_handle.strip().lower()
        base_damaged   = damaged_handle.strip().lower()

        # 1. FIND CANONICAL (REQUIRED)
        found_canonical = await find_existing_by_handle(base_canonical)

        if not found_canonical:
            logger.warning("[DuplicateCheck] Canonical NOT FOUND for '%s'", base_canonical)
            result["conflicts"]["canonical_missing"] = True
        else:
            result["existing_products"]["canonical"] = found_canonical

        # 2. FIND DAMAGED (MUST NOT EXIST for fresh create)
        found_damaged = await find_existing_by_handle(base_damaged)

        if found_damaged:
            logger.warning("[DuplicateCheck] Damaged ALREADY EXISTS for '%s'", base_damaged)
            result["conflicts"]["damaged_exists"] = True
            result["existing_products"]["damaged"] = found_damaged

        # 3. SUPABASE INVENTORY CHECK
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
                logger.warning("[DuplicateCheck] Inventory rows found for damaged root → conflict")
        except Exception as e:
            logger.warning("[DuplicateCheck] Supabase error: %s", e)

        # 4. FINAL RESOLUTION
        # Note: damaged_exists alone is NOT a blocking conflict for the confirm path
        # (we route to update instead). canonical_missing always blocks.
        has_blocking_conflict = result["conflicts"]["canonical_missing"]

        if has_blocking_conflict:
            result["status"] = "conflict"
            result["safe_to_create"] = False
            logger.info("[DuplicateCheck] → CONFLICT for '%s'", damaged_handle)
        else:
            logger.info("[DuplicateCheck] → CLEAR for '%s'", damaged_handle)

        return result

    except Exception as e:
        logger.error("[DuplicateCheck] Fatal error: %s", e)
        return {
            "status": "error",
            "error": str(e),
            "safe_to_create": False,
        }


async def create_damaged_product_with_duplicate_check(
    data: BulkCreateRequest,
) -> BulkCreateResult:
    """
    Duplicate-check → optional dry-run → create damaged product → return BulkCreateResult.
    """

    if data.inputs:
        raise RuntimeError(
            "create_damaged_product_with_duplicate_check does not accept inputs[]. "
            "Use bulk-create preview handler."
        )

    if not data.canonical_handle or not data.canonical_handle.strip():
        raise ValueError("canonical_handle is required for single-create path")

    seed_by_condition: dict[str, VariantSeed] = {}
    for seed in data.variants or []:
        key = (seed.condition or "").strip().lower()
        if key:
            seed_by_condition[key] = seed

    auto_damaged_handle = f"{data.canonical_handle.strip().lower()}-damaged"

    dup_result = await check_damaged_duplicate(
        canonical_handle=data.canonical_handle,
        damaged_handle=auto_damaged_handle,
    )

    if not dup_result.get("safe_to_create", False):
        logger.warning("[BulkCreate] Conflict detected for %s", auto_damaged_handle)

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
            logger.warning("[CreationLog] Failed to write conflict log: %s", e)

        return result

    if data.dry_run:
        logger.info("[BulkCreate] Dry run for %s", auto_damaged_handle)

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
            logger.warning("[CreationLog] Failed to write dry-run log: %s", e)

        return result

    # Step 4: Create damaged product
    created = await create_damaged_pair(
        canonical_handle=data.canonical_handle,
        variants=data.variants,
    )

    damaged = created.get("damaged", {}) or {}
    damaged_id = damaged.get("id")
    damaged_handle = auto_damaged_handle

    # Step 5: Extract CreatedVariantInfo[]
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

    # Step 6: Build BulkCreateResult
    result = BulkCreateResult(
        status="created",
        damaged_product_id=str(damaged_id) if damaged_id is not None else None,
        damaged_handle=damaged_handle,
        variants=extracted_variants,
        messages=["Damaged product created successfully."],
    )

    # Step 7: Initial inventory sync (best-effort)
    try:
        await _apply_initial_inventory(result)
    except Exception as e:
        logger.warning("[InventoryUpdate] Unexpected error during initial sync: %s", e)

    # Step 7b: Add to damaged-books collection if any variant is in stock
    if any((v.quantity_set or 0) > 0 for v in result.variants):
        try:
            await _add_to_damaged_collection(str(damaged_id))
        except Exception as e:
            logger.warning("[Collection] Failed to add new product to collection: %s", e)

    # Step 7c: Publish to all sales channels
    try:
        await _publish_product(str(damaged_id))
    except Exception as e:
        logger.warning("[Publish] Failed to publish new product %s: %s", damaged_id, e)

    # Step 7d: Set Print Books taxonomy category (Media > Books > Print Books)
    try:
        await _set_product_category(str(damaged_id))
    except Exception as e:
        logger.warning("[Taxonomy] Failed to set category on new product %s: %s", damaged_id, e)

    # Step 8: Write to creation_log
    try:
        await log_creation_event(data, result)
    except Exception as e:
        logger.warning("[CreationLog] Failed to write create log: %s", e)

    return result


async def _apply_initial_inventory(result: BulkCreateResult) -> None:
    """
    Best-effort initial inventory sync for newly created damaged variants.
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

            resp = await shopify_client.get(f"variants/{variant_id}.json")
            variant_obj = resp.get("body", {}).get("variant") or {}
            inventory_item_id = variant_obj.get("inventory_item_id")

            if not inventory_item_id:
                logger.warning(
                    "[InventoryUpdate] No inventory_item_id for variant %s; skipping.", variant_id,
                )
                continue

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
                    status, variant_id, payload,
                )
            else:
                logger.info(
                    "[InventoryUpdate] Set damaged variant %s inventory to %s at location %s",
                    variant_id, qty, location_id,
                )

        except Exception as e:
            logger.warning(
                "[InventoryUpdate] Failed to set inventory for variant_id=%s: %s",
                getattr(v, "variant_id", None), e,
            )


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
          light    = 15%
          moderate = 30%
          heavy    = 60%
      - Variant weight is inherited from the canonical product variant.
      - SKU: canonical variant sku (author) is copied to all damaged variants.
      - Barcode: synthetic — snake_case(canonical_handle) + '_' + condition_key
      - Inventory_management: 'shopify'
      - Inventory_policy: 'deny' (no overselling of damaged books)
    """

    import re

    # 1. Fetch canonical product
    canonical = await find_existing_by_handle(canonical_handle)
    if not canonical:
        raise RuntimeError(f"Canonical product not found for handle '{canonical_handle}'.")

    canonical_title = (canonical.get("title") or "").strip()

    # 2. Derive damaged title & handle
    m = re.split(r"[:;–—-]", canonical_title, maxsplit=1)
    base_title = m[0].strip()
    auto_damaged_title = f"{base_title}: Damaged"
    auto_damaged_handle = f"{canonical_handle.strip().lower()}-damaged"

    logger.info("[CreateDamagedPair] canonical=%s, damaged=%s", canonical_handle, auto_damaged_handle)

    # 3. Extract canonical fields
    canonical_variant = (canonical.get("variants") or [{}])[0]
    canonical_price_raw = canonical_variant.get("price") or "0.00"

    try:
        canonical_price = float(canonical_price_raw)
    except Exception:
        canonical_price = 0.0

    canonical_sku = canonical_variant.get("sku")
    vendor = canonical.get("vendor")
    product_type = canonical.get("product_type")

    # Weight inheritance — carry canonical variant weight to all damaged variants
    canonical_weight: float | None = None
    canonical_weight_unit: str = "g"
    try:
        w = canonical_variant.get("weight")
        if w is not None:
            canonical_weight = float(w) or None
        canonical_weight_unit = canonical_variant.get("weight_unit") or "g"
    except Exception:
        pass

    raw_tags = canonical.get("tags") or ""
    if isinstance(raw_tags, str):
        canonical_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    elif isinstance(raw_tags, list):
        canonical_tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    else:
        canonical_tags = []

    canonical_images = canonical.get("images", [])
    canonical_image_src = canonical_images[0]["src"] if canonical_images else None

    # Build variant seed lookup
    seed_by_condition: dict[str, VariantSeed] = {}
    for seed in variants or []:
        key = (seed.condition or "").strip().lower()
        if key:
            seed_by_condition[key] = seed

    # 4. Static damaged copy
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

    seo_title = f"{canonical_title} (Damaged)"
    seo_description = damaged_preamble

    # 5. Price override logic
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

    # 6. Build variant payloads — include weight inheritance
    variant_payloads: list[dict] = []

    for cond_key in ("light", "moderate", "heavy"):
        meta = CONDITION_META[cond_key]
        title = meta["title"]

        vp: dict = {
            "title": title,
            "option1": title,
            "sku": canonical_sku or "",
            "barcode": _make_barcode_for_condition(canonical_handle, cond_key),
            "price": compute_price_for_condition(cond_key),
            "inventory_management": "shopify",
            "inventory_policy": "deny",
        }

        # Inherit weight from canonical variant
        if canonical_weight is not None:
            vp["weight"] = canonical_weight
            vp["weight_unit"] = canonical_weight_unit

        variant_payloads.append(vp)

    # 7. Build product payload
    payload = {
        "product": {
            "title": auto_damaged_title,
            "handle": auto_damaged_handle,
            "status": "draft",
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

    # 8. Create damaged product via REST
    resp = await shopify_client.post("products.json", data=payload)
    damaged = resp.get("body", {}).get("product")
    if damaged and "id" in damaged:
        damaged["id"] = str(damaged["id"])
    if not damaged:
        raise RuntimeError(f"Failed to create damaged product {auto_damaged_handle}")

    logger.info("[CreateDamagedPair] Created damaged id=%s handle=%s", damaged.get("id"), auto_damaged_handle)

    return {"canonical": canonical, "damaged": damaged}


# --------------------------------------------------------
# Misc helpers (publish/unpublish)
# --------------------------------------------------------

async def get_product_by_id(product_id: str) -> dict:
    try:
        path = f"products/{product_id}.json"
        response = await shopify_client.get(path)
        return response.get("body", {}).get("product", {})
    except Exception as e:
        logger.error("Error fetching product %s: %s", product_id, e)
        raise


async def set_product_publish_status(product_id: str, should_publish: bool) -> dict:
    try:
        published_at = datetime.utcnow().isoformat() if should_publish else None
        path = f"products/{product_id}.json"
        payload = {
            "product": {
                "id": product_id,
                "published_at": published_at,
            }
        }
        response = await shopify_client.put(path, data=payload)
        return response.get("body", {}).get("product", {})
    except Exception as e:
        action = "publishing" if should_publish else "unpublishing"
        logger.error("Error %s product %s: %s", action, product_id, e)
        raise


# --------------------------------------------------------
# Deprecated legacy helpers
# --------------------------------------------------------

def parse_damaged_handle(handle: str) -> tuple[str, str]:
    logger.warning("parse_damaged_handle is deprecated.")
    import re
    h = (handle or "").lower()
    m = re.match(r"^(?P<base>.+)-(?:hurt|used|damaged|damage)-(light|moderate|mod|heavy)$", h)
    if m:
        return m.group("base"), m.group(2)
    m = re.match(r"^(?P<base>.+)-(light|moderate|mod|heavy)-damage$", h)
    if m:
        return m.group("base"), m.group(2)
    return handle, None

def is_used_book_handle(handle: str) -> bool:
    logger.warning("is_used_book_handle is deprecated.")
    base, condition = parse_damaged_handle(handle)
    return condition is not None

def get_new_book_handle_from_used(used_handle: str) -> str:
    logger.warning("get_new_book_handle_from_used is deprecated.")
    base, condition = parse_damaged_handle(used_handle)
    return base

def parse_condition_from_handle(handle: str) -> str | None:
    logger.warning("parse_condition_from_handle is deprecated.")
    _, cond = parse_damaged_handle(handle)
    return cond

def is_damaged_handle(handle: str) -> bool:
    return is_used_book_handle(handle)


# ---------------------------------------------------------------------------
# Live product details for sidebar enrichment (Phase 6B)
# ---------------------------------------------------------------------------

async def get_damaged_product_details(product_id: str) -> dict:
    """
    Fetch live Shopify data for the DamagedBooksTable sidebar.
    Returns publishing status per channel, taxonomy category, and
    per-variant weight + inventory quantity.

    Called lazily on sidebar open — always live, never cached.
    """
    raw_id = str(product_id).split("/")[-1]
    product_gid = f"gid://shopify/Product/{raw_id}"

    query = """
    query DamagedProductDetails($id: ID!) {
      product(id: $id) {
        id
        status
        publishedAt
        category {
          id
          name
          fullName
        }
        resourcePublications(first: 10) {
          edges {
            node {
              publication { id name }
              isPublished
              publishDate
            }
          }
        }
        variants(first: 5) {
          edges {
            node {
              id
              title
              inventoryQuantity
              inventoryItem {
                measurement {
                  weight { value unit }
                }
              }
            }
          }
        }
      }
    }
    """

    resp = await shopify_client.graphql(query, {"id": product_gid})
    body = resp.get("body", resp)

    gql_errors = body.get("errors") or []
    if gql_errors:
        logger.warning("[ProductDetails] GQL errors for %s: %s", product_gid, gql_errors)
        raise RuntimeError(f"Shopify GQL errors: {gql_errors}")

    product = (body.get("data") or {}).get("product") or {}
    if not product:
        raise RuntimeError(f"Product not found: {product_gid}")

    # ── Channels ──────────────────────────────────────────────────────────────
    channels = []
    for edge in ((product.get("resourcePublications") or {}).get("edges") or []):
        node = edge.get("node") or {}
        pub  = node.get("publication") or {}
        channels.append({
            "name":         pub.get("name") or "Unknown",
            "is_published": bool(node.get("isPublished")),
            "publish_date": node.get("publishDate"),
        })

    online_store_published = any(
        c["name"].lower() in ("online store", "online-store") and c["is_published"]
        for c in channels
    )

    # ── Variants + weight ─────────────────────────────────────────────────────
    variants = []
    weight_value: float | None = None
    weight_unit: str | None = None

    for edge in ((product.get("variants") or {}).get("edges") or []):
        node = edge.get("node") or {}
        w_node = (
            (node.get("inventoryItem") or {})
            .get("measurement") or {}
        ).get("weight") or {}

        w_val  = w_node.get("value")
        w_unit = (w_node.get("unit") or "").upper()

        if weight_value is None and w_val is not None:
            weight_value = float(w_val)
            weight_unit  = _WEIGHT_UNIT_MAP.get(w_unit, w_unit.lower() or "g")

        variants.append({
            "id":                 str(node.get("id") or "").split("/")[-1],
            "title":              node.get("title"),
            "inventory_quantity": node.get("inventoryQuantity"),
            "weight":             float(w_val) if w_val is not None else None,
            "weight_unit":        _WEIGHT_UNIT_MAP.get(w_unit, w_unit.lower() or "g"),
        })

    # ── Category ──────────────────────────────────────────────────────────────
    cat = product.get("category") or {}
    category = {
        "id":        cat.get("id"),
        "name":      cat.get("name"),
        "full_name": cat.get("fullName"),
    } if cat.get("id") else None

    return {
        "product_id":             raw_id,
        "status":                 (product.get("status") or "").lower(),
        "published_at":           product.get("publishedAt"),
        "online_store_published": online_store_published,
        "channels":               channels,
        "category":               category,
        "weight":                 weight_value,
        "weight_unit":            weight_unit,
        "variants":               variants,
    }