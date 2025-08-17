# services/damaged_inventory_repo.py
from services.supabase_client import supabase

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
    # Function lives in schema "damaged" but is callable as 'damaged_upsert_inventory'
    # (you set search_path inside the function).
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
    q = supabase.table("damaged.inventory_view").select("*").limit(limit)
    if in_stock is True:
        q = q.gt("available", 0)
    elif in_stock is False:
        q = q.eq("available", 0)
    return q.execute()