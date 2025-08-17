# services/cron_service.py
import os
import asyncio
import logging
from services.supabase_client import get_client
from services.shopify_client import shopify_client
from services import damaged_inventory_repo

logger = logging.getLogger(__name__)

supabase = get_client()

SHOPIFY_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID")  # required for GQL inventoryLevel

async def reconcile_damaged_inventory(batch_limit: int = 200):
    """
    Pull current rows from damaged.inventory, hit Shopify GQL to confirm availability
    for each inventory_item_id at your location, and upsert back.
    """
    if not SHOPIFY_LOCATION_ID:
        logger.warning("SHOPIFY_LOCATION_ID not set; reconcile aborted.")
        return

    # 1) fetch rows
    res = supabase.table("damaged.inventory").select(
        "inventory_item_id, product_id, variant_id, handle, condition, title, sku, barcode"
    ).limit(batch_limit).execute()

    rows = res.data or []
    logger.info(f"[Reconcile] inspecting {len(rows)} rows")

    for r in rows:
        inv_id = int(r["inventory_item_id"])
        try:
            # 2) GQL inventory level by (inventory_item_id, location_id)
            # You can implement a helper in shopify_client if you prefer.
            gql = """
            query($inventoryItemId: ID!, $locationId: ID!) {
              inventoryLevel(inventoryItemId: $inventoryItemId, locationId: $locationId) {
                available
              }
            }
            """
            variables = {
                "inventoryItemId": f"gid://shopify/InventoryItem/{inv_id}",
                "locationId": f"gid://shopify/Location/{SHOPIFY_LOCATION_ID}",
            }
            resp = await shopify_client.graphql(gql, variables)  # assumes you have .graphql(method) already
            available = (resp.get("body", {}) or {}).get("data", {}) \
                .get("inventoryLevel", {}) \
                .get("available", 0)

            # 3) upsert with source='reconcile'
            await damaged_inventory_repo.upsert(
                inventory_item_id=inv_id,
                product_id=int(r["product_id"]),
                variant_id=int(r["variant_id"]),
                handle=r["handle"],
                condition=r.get("condition"),
                available=int(available or 0),
                source="reconcile",
                title=r.get("title"),
                sku=r.get("sku"),
                barcode=r.get("barcode"),
            )
        except Exception as e:
            logger.warning(f"[Reconcile] inventory_item_id={inv_id}: {e}")

async def run_forever(interval_seconds: int = 1800):
    while True:
        await reconcile_damaged_inventory()
        await asyncio.sleep(interval_seconds)