# /services/inventory_service.py

import logging
from typing import Optional
from services.shopify_client import shopify_client

logger = logging.getLogger(__name__)

def _extract_condition_from_variant(variant: dict) -> Optional[str]:
    """
    Extract the variant option related to condition/damage.
    Priority:
      1. Look for selectedOptions with name 'Condition' or containing 'damage'.
      2. Fallback to option1/2/3 if they contain 'damage'.
      3. Fallback to variant title if it looks like a condition string.
    """
    try:
        # Admin GraphQL returns selectedOptions [{name, value}]
        for opt in (variant.get("selectedOptions") or []):
            name = (opt.get("name") or "").strip().lower()
            val = (opt.get("value") or "").strip()
            if name == "condition" or "damage" in name:
                return val
            # In some stores, option might be named differently but value has damage keyword
            if "damage" in val.lower():
                return val

        # Fallback for older stores using option1/2/3
        for k in ("option1", "option2", "option3"):
            v = variant.get(k)
            if isinstance(v, str) and "damage" in v.lower():
                return v

        # Fallback to variant title if it contains damage info
        title = variant.get("title")
        if isinstance(title, str) and "damage" in title.lower():
            return title

    except Exception as e:
        logger.warning(f"[InventoryService] Failed to extract condition: {e}. Variant={variant}")

    return None

async def is_variant_in_stock(variant_id: str, inventory_item_id: str, *, available_hint: Optional[int] = None) -> bool:
    """
    Decide stock status.
    Prefer the `available` value carried by the inventory_levels/update webhook.
    If not provided, fall back to querying inventory_levels/locations.
    """
    try:
        # Fast path: webhook provided the 'available' quantity
        if available_hint is not None:
            return int(available_hint) > 0

        # Fallback: fetch inventory level for this inventory_item_id at all locations
        resp = await shopify_client.get(
            "inventory_levels.json",
            query={"inventory_item_ids": inventory_item_id}
        )
        body = resp.get("body") or {}
        levels = body.get("inventory_levels", []) or []
        total_available = 0
        for lvl in levels:
            try:
                total_available += int(lvl.get("available") or 0)
            except Exception:
                continue
        return total_available > 0

    except Exception as e:
        logger.warning(f"[InventoryService] stock check fallback error for inventory_item_id={inventory_item_id}: {e}")
        # On error, be conservative: treat as not in stock so we don't accidentally publish
        return False


# ---------------------------------------------------------------
# Additional resolver for inventory_item_id to variant/product info
async def resolve_by_inventory_item_id(inventory_item_id: int, location_gid: str) -> dict:
    """
    Resolve Shopify variant/product info from an inventory_item_id and return:
      {
        "available": int,
        "inventory_item_id": int,
        "variant": {
           "id": str_gid, "sku": str|None, "barcode": str|None,
           "title": str|None,
           "selectedOptions": [{"name","value"}, ...],
           "condition": "Light Damage" | "Moderate Damage" | "Heavy Damage" | None
        },
        "product": { "id": str_gid, "handle": str, "title": str }
      }
    """
    gql = """
    query VariantByInventoryItem($inventoryItemId: ID!, $locationId: ID!) {
      inventoryItem(id: $inventoryItemId) {
        id
        variant {
          id
          sku
          barcode
          title
          selectedOptions { name value }
          product { id handle title }
        }
        inventoryLevels(first: 1, query: $locationId) {
          edges { node { available } }
        }
      }
    }
    """
    variables = {
        "inventoryItemId": f"gid://shopify/InventoryItem/{inventory_item_id}",
        "locationId": location_gid,
    }
    resp = await shopify_client.graphql(gql, variables)
    data = ((resp or {}).get("body") or {}).get("data") or {}
    item = data.get("inventoryItem") or {}
    edges = (((item.get("inventoryLevels") or {}).get("edges")) or [])
    available = (edges[0]["node"]["available"] if edges else 0) or 0
    variant = item.get("variant") or {}
    product = (variant.get("product") or {})
    condition = _extract_condition_from_variant(variant)
    variant["condition"] = condition
    try:
        inventory_item_id_int = int(inventory_item_id)
    except Exception:
        # If caller passed a string gid number, do a best-effort cast
        inventory_item_id_int = int(str(inventory_item_id).split("/")[-1])
    return {
        "available": int(available),
        "inventory_item_id": inventory_item_id_int,
        "variant": variant,
        "product": product,
    }