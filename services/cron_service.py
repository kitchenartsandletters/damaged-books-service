# services/cron_service.py
from services.supabase_client import get_client
from services.shopify_client import shopify_client
import logging, asyncio, os
from services import damaged_inventory_repo

logger = logging.getLogger(__name__)
supabase = get_client()
SHOPIFY_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID")

def _to_gid(kind: str, v: str | int | None) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s if s.startswith("gid://") else f"gid://shopify/{kind}/{s}"

async def reconcile_damaged_inventory(batch_limit: int = 200):
    inspected = 0
    updated = 0
    skipped = 0

    if not SHOPIFY_LOCATION_ID:
        return {"inspected": 0, "updated": 0, "skipped": 0, "note": "missing SHOPIFY_LOCATION_ID"}

    # Normalize once
    location_gid = _to_gid("Location", SHOPIFY_LOCATION_ID)

    res = supabase.schema("damaged").from_("inventory").select(
        "inventory_item_id, product_id, variant_id, handle, condition, title, sku, barcode, available"
    ).limit(batch_limit).execute()
    rows = res.data or []

    for r in rows:
        inspected += 1
        inv_id = int(r["inventory_item_id"])
        try:
            inv_gid = _to_gid("InventoryItem", inv_id)
            gql = """
            query($inventoryItemId: ID!, $locationId: ID!) {
              inventoryLevel(inventoryItemId: $inventoryItemId, locationId: $locationId) {
                available
              }
            }
            """
            variables = {"inventoryItemId": inv_gid, "locationId": location_gid}

            data = await shopify_client.graph(gql, variables)
            node = (data or {}).get("data", {}).get("inventoryLevel")
            gql_available = None if node is None else node.get("available")

            # If GQL didn’t return a number, do NOT stomp the existing value
            final_available = int(gql_available) if isinstance(gql_available, int) else int(r.get("available") or 0)

            # Enrich sku/barcode if missing
            sku = r.get("sku")
            barcode = r.get("barcode")
            if (not sku or not barcode) and r.get("variant_id"):
                try:
                    v = await shopify_client.get_variant_by_id(str(r["variant_id"]))
                    if v:
                        if not sku:
                            sku = v.get("sku") or None
                        if not barcode:
                            barcode = v.get("barcode") or None
                except Exception as e:
                    logger.info(f"[Reconcile] variant enrich failed variant_id={r.get('variant_id')}: {e}")

            damaged_inventory_repo.upsert(
                inventory_item_id=inv_id,
                product_id=int(r["product_id"]),
                variant_id=int(r["variant_id"]),
                handle=r["handle"],
                condition=r.get("condition"),
                available=final_available,
                source="reconcile",
                title=r.get("title"),
                sku=(sku or None),
                barcode=(barcode or None),
            )
            updated += 1

        except Exception as e:
            logger.info(f"[Reconcile] skip inventory_item_id={inv_id}: {e}")
            skipped += 1

    # Persist a single summary row
    try:
        supabase.schema("damaged").from_("reconcile_log").insert({
            "inspected": inspected,
            "updated": updated,
            "skipped": skipped,
            "note": None,
        }).execute()
    except Exception as e:
        logger.warning(f"Failed to persist reconcile log: {e}")

    return {"inspected": inspected, "updated": updated, "skipped": skipped, "note": None}

async def run_forever(interval_seconds: int = 1800):
    while True:
        await reconcile_damaged_inventory()
        await asyncio.sleep(interval_seconds)