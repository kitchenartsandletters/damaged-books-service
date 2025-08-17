# services/damaged_inventory_repo.py
from services.supabase_client import get_client

supabase = get_client()

async def upsert(
    inventory_item_id: int,
    product_id: int,
    variant_id: int,
    handle: str,
    condition: str | None,
    available: int,
    source: str = "webhook",
    title: str | None = None,
    sku: str | None = None,
    barcode: str | None = None,
):
    # Calls SQL function damaged.damaged_upsert_inventory (exposed as damaged_upsert_inventory)
    return supabase.rpc(
        "damaged_upsert_inventory",
        {
            "_inventory_item_id": inventory_item_id,
            "_product_id": product_id,
            "_variant_id": variant_id,
            "_handle": handle or "",
            "_condition": condition,
            "_available": available,
            "_source": source,
            "_title": title,
            "_sku": sku,
            "_barcode": barcode,
        },
    ).execute()

def list_view(limit: int = 200, in_stock: bool | None = None):
    # Read from `damaged.inventory_view` via explicit schema
    q = supabase.schema("damaged").from_("inventory_view").select("*").limit(limit)
    if in_stock is True:
        q = q.gt("available", 0)
    elif in_stock is False:
        q = q.eq("available", 0)
    return q.execute()