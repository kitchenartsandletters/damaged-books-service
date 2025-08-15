# /services/inventory_service.py

import logging
from typing import Optional
from services.shopify_client import shopify_client

logger = logging.getLogger(__name__)

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