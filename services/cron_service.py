# services/cron_service.py
from services.supabase_client import get_client
from services.shopify_client import shopify_client  # Add this import or adjust as needed
import logging
import asyncio
from services import damaged_inventory_repo, product_service, notification_service
import os
from services.used_book_manager import apply_product_rules_with_product
from services import seo_service
from services.inventory_service import resolve_by_inventory_item_id
from services.conditions import normalize_condition_key

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
    errors: list[str] = []

    if not SHOPIFY_LOCATION_ID:
        return {"inspected": 0, "updated": 0, "skipped": 0, "note": "missing SHOPIFY_LOCATION_ID"}

    # Normalize once
    location_gid = _to_gid("Location", SHOPIFY_LOCATION_ID)

    res = supabase.schema("damaged").from_("inventory").select(
        "inventory_item_id, product_id, variant_id, handle, condition, condition_raw, condition_key, title, sku, barcode, available"
    ).limit(batch_limit).execute()
    rows = res.data or []   

    touched = set()
    for r in rows:
        inspected += 1
        inv_id = int(r["inventory_item_id"])
        product_id = int(r["product_id"])
        handle = r["handle"]
        try:
            resp = await resolve_by_inventory_item_id(inv_id, location_gid)
            available = resp.get("available")
            if available is None:
                available = r.get("available", 0)
            sku = None
            barcode = None
            condition_raw = None
            condition_key = None
            condition = None

            variant = resp.get("variant", {})
            sku = variant.get("sku")
            barcode = variant.get("barcode")
            selected_options = variant.get("selectedOptions", [])
            for option in selected_options:
                if option.get("name") == "Condition":
                    condition_raw = option.get("value")
                    break

            # Canonicalise ("Light Damage" -> "light"). Unknown values raise.
            condition_key = normalize_condition_key(condition_raw)
            if condition_key is None:
                # No Condition option on the variant: keep what we already
                # know rather than NULLing a valid stored condition.
                condition_key = normalize_condition_key(r.get("condition_key") or r.get("condition"))
                condition_raw = condition_raw or r.get("condition_raw")
                logger.warning(f"[Reconcile] inventory_item_id={inv_id} has no Condition option; keeping stored condition={condition_key!r}")
            condition = condition_key

            logger.info(f"Upserting inventory item {inv_id} with condition={condition_key}, condition_raw={condition_raw}, condition_key={condition_key}, available={available}, sku={sku}, barcode={barcode}")

            damaged_inventory_repo.upsert(
                inventory_item_id=inv_id,
                product_id=product_id,
                variant_id=int(r["variant_id"]),
                handle=handle,
                condition=condition_key,
                condition_raw=condition_raw,
                condition_key=condition_key,
                available=int(available or 0),
                source="reconcile",
                title=r.get("title"),
                sku=sku,
                barcode=barcode,
            )
            touched.add((product_id, handle))
            updated += 1

        except Exception as e:
            logger.error(f"[Reconcile] FAILED inventory_item_id={inv_id}: {type(e).__name__}: {e}")
            skipped += 1
            errors.append(f"{type(e).__name__}: {e}")
    # Apply product-level rules once per damaged product we touched
    # NOTE: this loop was unreachable from 2025-09-22 to the condition fix
    # (no row was ever "touched"), which hid the missing await below.
    rule_failures = 0
    for (pid, handle) in touched:
        try:
            canonical = await seo_service.resolve_canonical_handle(handle)
            await apply_product_rules_with_product(str(pid), handle, canonical)
        except Exception as e:
            rule_failures += 1
            logger.error(f"[Reconcile] Failed to apply product rules for {handle}: {type(e).__name__}: {e}")

    note = None
    if errors:
        # Persist a summary so reconcile_log alone shows *why* rows failed.
        first = errors[0][:300]
        distinct = len(set(errors))
        note = f"FAILED {skipped}/{inspected}; {distinct} distinct error(s); first: {first}"
        logger.error(f"[Reconcile] {note}")
    if rule_failures:
        note = (note + "; " if note else "") + f"product rules failed for {rule_failures}/{len(touched)} products"

    try:
        supabase.schema("damaged").from_("reconcile_log").insert({
            "inspected": inspected,
            "updated": updated,
            "skipped": skipped,
            "note": note,
        }).execute()
    except Exception as e:
        logger.error(f"Failed to persist reconcile log: {e}")

    return {
        "inspected": inspected,
        "updated": updated,
        "skipped": skipped,
        "failed": skipped,
        "products_touched": len(touched),
        "rule_failures": rule_failures,
        "note": note,
    }                           

async def run_forever(interval_seconds: int = 1800):
    while True:
        await reconcile_damaged_inventory()
        await asyncio.sleep(interval_seconds)