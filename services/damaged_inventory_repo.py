# services/damaged_inventory_repo.py
from services.supabase_client import get_client

supabase = get_client()

def upsert(
    inventory_item_id: int,
    product_id: int,
    variant_id: int,
    handle: str,
    condition: str | None,  # ← legacy parameter, ignored in upsert and replaced by condition_key
    available: int,
    condition_raw: str | None = None,
    condition_key: str | None = None,
    source: str = "webhook",
    title: str | None = None,
    sku: str | None = None,
    barcode: str | None = None,
):
    return supabase.schema("damaged").rpc(
        "damaged_upsert_inventory",
        {
            "_inventory_item_id": inventory_item_id,
            "_product_id": product_id,
            "_variant_id": variant_id,
            "_handle": handle or "",
            "_condition": condition_key,          # ← legacy column now set to normalized condition_key
            "_condition_raw": condition_raw,      # ← new
            "_condition_key": condition_key,      # ← new
            "_available": available,
            "_source": source,
            "_title": title,
            "_sku": sku,
            "_barcode": barcode,
        },
    ).execute()

def list_view(limit: int = 200, in_stock: bool | None = None):
    q = supabase.schema("damaged").from_("inventory_view").select("*").limit(limit)
    if in_stock is True:
        q = q.gt("available", 0)
    elif in_stock is False:
        q = q.eq("available", 0)
    return q.execute()