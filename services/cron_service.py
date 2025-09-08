# services/cron_service.py
from services.supabase_client import get_client
from services.shopify_client import shopify_client  # Add this import or adjust as needed
import logging
import asyncio
from services import damaged_inventory_repo, product_service, notification_service
from services.inventory_service import resolve_by_inventory_item_id
from services.used_book_manager import apply_product_rules_with_product
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
    touched = set()  # set of tuples (product_id:int, handle:str)
    for r in rows:
        inspected += 1
        inv_id = int(r["inventory_item_id"])
        try:
            resv = await resolve_by_inventory_item_id(inv_id, f"gid://shopify/Location/{SHOPIFY_LOCATION_ID}")
            variant = resv.get("variant") or {}
            product = resv.get("product") or {}
            available = int(resv.get("available") or 0)
            condition = variant.get("condition")
            product_id = int((product.get("id") or "gid://shopify/Product/0").split("/")[-1])
            variant_id = int((variant.get("id") or "gid://shopify/ProductVariant/0").split("/")[-1])
            handle = product.get("handle") or ""
            title = product.get("title")
            sku = variant.get("sku")
            barcode = variant.get("barcode")
            await damaged_inventory_repo.upsert(
                inventory_item_id=inv_id,
                product_id=product_id,
                variant_id=variant_id,
                handle=handle,
                condition=condition,
                available=available,
                source="reconcile",
                title=title,
                sku=sku,
                barcode=barcode,
            )
            if handle:
                touched.add((product_id, handle))
            updated += 1
        except Exception as e:
            skipped += 1
    # Apply product-level rules once per damaged product we touched
    for (pid, handle) in touched:
        if handle.endswith("-damaged"):
            canonical = handle.removesuffix("-damaged")
            try:
                await apply_product_rules_with_product(str(pid), handle, canonical)
            except Exception as e:
                logger.warning(f"Failed to apply product rules for {handle}: {e}")

    note = "missing SHOPIFY_LOCATION_ID" if not SHOPIFY_LOCATION_ID else None

    # Persist reconcile log after processing all rows
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
        "note": note,
    }
    
async def run_forever(interval_seconds: int = 1800):
    while True:
        await reconcile_damaged_inventory()
        await asyncio.sleep(interval_seconds)