# services/cron_service.py
from services.supabase_client import get_client
from services.shopify_client import shopify_client  # Add this import or adjust as needed
import logging
import asyncio
from services import damaged_inventory_repo, product_service, notification_service
import os

logger = logging.getLogger(__name__)

supabase = get_client()

SHOPIFY_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID")  # required for GQL inventoryLevel

async def reconcile_damaged_inventory(batch_limit: int = 200):
    inspected = 0
    updated = 0
    skipped = 0

    if not SHOPIFY_LOCATION_ID:
        # Don’t 500—just report why nothing ran
        return {"inspected": 0, "updated": 0, "skipped": 0, "note": "missing SHOPIFY_LOCATION_ID"}

    res = supabase.schema("damaged").from_("inventory").select(
        "inventory_item_id, product_id, variant_id, handle, condition, title, sku, barcode"
    ).limit(batch_limit).execute()
    rows = res.data or []   

    rows = res.data or []
    for r in rows:
        inspected += 1
        inv_id = int(r["inventory_item_id"])
        try:
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
            resp = await shopify_client.graphql(gql, variables)
            available = ((resp.get("body", {}) or {}).get("data", {})
                         .get("inventoryLevel", {})
                         .get("available", 0))
            
            # Enrich sku/barcode if missing
            sku = r.get("sku")
            barcode = r.get("barcode")
            if (not sku or not barcode) and r.get("variant_id"):
                try:
                    variant = await product_service.get_variant_by_id(str(r["variant_id"]))
                    if variant:
                        sku = sku or (variant.get("sku") or None)
                        barcode = barcode or (variant.get("barcode") or None)
                except Exception as e:
                    logger.info(f"[Reconcile] variant enrich failed variant_id={r.get('variant_id')}: {e}")

            damaged_inventory_repo.upsert(
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
            updated += 1
        except Exception as e:
            skipped += 1

        # after loop: write a single reconcile_log row and return summary
        note = "missing SHOPIFY_LOCATION_ID" if not SHOPIFY_LOCATION_ID else None
        try:
            supabase.schema("damaged").from_("reconcile_log").insert({
                "inspected": inspected,
                "updated": updated,
                "skipped": skipped,
                "note": note,
            }).execute()
        except Exception as e:
            logger.warning(f"Failed to persist reconcile log: {e}")
    
        return {
            "inspected": inspected,
            "updated": updated,
            "skipped": skipped,
            "note": note
        }                       
    
async def run_forever(interval_seconds: int = 1800):
    while True:
        await reconcile_damaged_inventory()
        await asyncio.sleep(interval_seconds)