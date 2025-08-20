# services/cron_service.py
from services.supabase_client import get_client
from services.shopify_client import shopify_client
import logging, asyncio, os
from services import damaged_inventory_repo  # use repo directly

logger = logging.getLogger(__name__)
supabase = get_client()
SHOPIFY_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID")

async def reconcile_damaged_inventory(batch_limit: int = 200):
    inspected = 0
    updated = 0
    skipped = 0

    if not SHOPIFY_LOCATION_ID:
        # Do not 500 — just report
        return {"inspected": 0, "updated": 0, "skipped": 0, "note": "missing SHOPIFY_LOCATION_ID"}

    res = supabase.schema("damaged").from_("inventory").select(
        "inventory_item_id, product_id, variant_id, handle, condition, title, sku, barcode"
    ).limit(batch_limit).execute()
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
            # ✅ correct client method name
            data = await shopify_client.graph(gql, variables)
            available = ((data or {}).get("data", {})
                         .get("inventoryLevel", {})
                         .get("available", 0)) or 0

            # ✅ enrich sku/barcode if missing
            sku = r.get("sku")
            barcode = r.get("barcode")
            if (not sku or not barcode) and r.get("variant_id"):
                try:
                    v = await shopify_client.get_variant_by_id(str(r["variant_id"]))
                    if v:
                        sku = sku or (v.get("sku") or None)
                        barcode = barcode or (v.get("barcode") or None)
                except Exception as e:
                    logger.info(f"[Reconcile] variant enrich failed variant_id={r.get('variant_id')}: {e}")

            # ✅ pass the enriched values
            damaged_inventory_repo.upsert(
                inventory_item_id=inv_id,
                product_id=int(r["product_id"]),
                variant_id=int(r["variant_id"]),
                handle=r["handle"],
                condition=r.get("condition"),
                available=int(available),
                source="reconcile",
                title=r.get("title"),
                sku=(sku if (sku is not None and sku != "") else None),
                barcode=(barcode if (barcode is not None and barcode != "") else None),
            )
            updated += 1
        except Exception as e:
            logger.info(f"[Reconcile] skip inventory_item_id={inv_id}: {e}")
            skipped += 1

    # ✅ write a single reconcile_log row after the loop
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

    return {"inspected": inspected, "updated": updated, "skipped": skipped, "note": note}

async def run_forever(interval_seconds: int = 1800):
    while True:
        await reconcile_damaged_inventory()
        await asyncio.sleep(interval_seconds)