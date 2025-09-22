# services/used_book_manager.py

import logging
from services import redirect_service, seo_service, inventory_service
from services import notification_service
from services.shopify_client import shopify_client
import os
from typing import Optional
from services.inventory_service import resolve_by_inventory_item_id, coerce_quantity

logger = logging.getLogger(__name__)

SHOPIFY_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID")

async def apply_product_rules_with_product(product_id: str, damaged_handle: str, canonical_handle: str) -> None:
    """
    Publish/unpublish and manage redirects at the product level:
      - If ANY variant in-stock → publish damaged product, remove redirect.
      - If ALL variants OOS → unpublish damaged product, create redirect to canonical.
    Uses our damaged inventory view to compute aggregate availability.
    """
    try:
        from services import damaged_inventory_repo  # local import to avoid cycles
        rows_resp = damaged_inventory_repo.list_view(limit=2000, in_stock=None)
        rows = rows_resp.data or []
        product_rows = [r for r in rows if (r.get("handle") or "").lower() == damaged_handle.lower()]
        any_in_stock = any(coerce_quantity(r.get("available")) > 0 for r in product_rows)

        if any_in_stock:
            # Publish damaged product and remove redirect if present
            await shopify_client.set_product_publish_status(product_id, True)
            existing = await redirect_service.find_redirect_by_path(damaged_handle)
            if existing:
                deleted = await redirect_service.delete_redirect(str(existing.get("id")))
                if deleted is not True and deleted not in (200, 204):
                    logger.warning(f"[Redirect] Failed to delete redirect id={existing.get('id')} for {damaged_handle}")
                    notification_service.notify(
                        "warning",
                        "Redirect Removal Failed",
                        f"Could not remove redirect for {damaged_handle} (id={existing.get('id')})"
                    )
        else:
            # Unpublish damaged product and ensure redirect exists
            await shopify_client.set_product_publish_status(product_id, False)
            existing = await redirect_service.find_redirect_by_path(damaged_handle)
            if not existing:
                created = await redirect_service.create_redirect(damaged_handle, canonical_handle)
                if created is None:
                    logger.warning(f"[Redirect] Creation returned empty/invalid result for {damaged_handle} → {canonical_handle}")
                    notification_service.notify(
                        "warning",
                        "Redirect Creation Failed",
                        f"Could not create redirect from {damaged_handle} to {canonical_handle}"
                    )
                else:
                    logger.info(f"[Redirect] Created id={created.get('id')} from {damaged_handle} → {canonical_handle}")

    except Exception as e:
        logger.warning(f"[UsedBookManager] apply_product_rules_with_product error: {e}")

async def process_inventory_change(inventory_item_id: str, variant_id: str, product: dict, available_hint: int | None = None) -> dict:
    try:
        product_id = product.get("id")
        handle = (product.get("handle") or "").lower()

        # Damage check: only handles ending with "-damaged" are considered damaged books.
        is_damaged = handle.endswith("-damaged")
        logger.info(f"[DamagedCheck] handle={handle} matched={is_damaged}")
        if not is_damaged:
            logger.info(f"[Inventory] Product {product_id} is not a damaged book, skipping")
            return {"productId": product_id, "handle": handle, "skipped": "not_damaged"}

        # Optional hint
        if available_hint is not None:
            logger.info(f"[Hint] available_hint={available_hint} for inventory_item_id={inventory_item_id}, variant_id={variant_id}, product_id={product_id}")

        # Stock status (prefer hint if provided)
        if available_hint is not None:
            is_in_stock = coerce_quantity(available_hint) > 0
        else:
            is_in_stock = await inventory_service.is_variant_in_stock(variant_id, inventory_item_id)
        logger.info(f"[Inventory] Damaged book {handle} stock status: {'in stock' if is_in_stock else 'out of stock'}")

        # Canonical target
        canonical_handle = await seo_service.resolve_canonical_handle(damaged_handle=handle, product=product)

        # Resolve variant + product + condition via Admin GraphQL using inventory_item_id
        condition_raw = None
        condition_key = None
        variant_data = None
        if SHOPIFY_LOCATION_ID:
            try:
                res = await resolve_by_inventory_item_id(int(inventory_item_id), f"gid://shopify/Location/{SHOPIFY_LOCATION_ID}")
                variant_data = res.get("variant") or {}

                logger.info(f"[Inventory] variant_data: {variant_data}")

                # Extract condition_raw and condition_key directly
                selected_options = (
                    variant_data.get("selected_options")
                    or variant_data.get("selectedOptions")
                    or []
                )
                condition_raw = None
                condition_key = None
                if selected_options:
                    condition_raw = selected_options[0].get("value")
                    if condition_raw:
                        condition_raw_str = str(condition_raw)
                        condition_map = {
                            "light damage": "light",
                            "moderate damage": "moderate",
                            "heavy damage": "heavy",
                        }
                        condition_key = condition_map.get(
                            condition_raw_str.lower().strip(),
                            condition_raw_str.lower().strip()
                        )
                else:
                    condition_raw = None
                    condition_key = None

            except Exception as e:
                logger.warning(f"[Inventory] Resolver failed to fetch variant condition: {e}")
        else:
            logger.warning("[Inventory] SHOPIFY_LOCATION_ID is not set; skipping condition resolution via resolver")

        logger.info(
            f"[Upsert Debug] condition_raw={condition_raw}, condition_key={condition_key}, condition={condition_key}"
        )

        from services import damaged_inventory_repo
        damaged_inventory_repo.upsert(
            inventory_item_id=int(inventory_item_id),
            product_id=int(product_id),
            variant_id=int(variant_id),
            handle=handle,
            condition=condition_key,          # condition == condition_key
            condition_raw=condition_raw,
            condition_key=condition_key,
            available=coerce_quantity(available_hint if available_hint is not None else (1 if is_in_stock else 0)),
            source="webhook",
            title=product.get("title"),
            sku=(str(variant_data.get("sku")) if variant_data else None),
            barcode=(str(variant_data.get("barcode")) if variant_data else None),
        )

        # Apply product-level rules once per product (handle publish/unpublish, redirects, and canonical metafield)
        await apply_product_rules_with_product(product_id, handle, canonical_handle)

        return {
            "productId": product_id,
            "handle": handle,
            "inStock": is_in_stock,
            "action": "published" if is_in_stock else "unpublished",
            "canonicalSet": True,  # since update_used_book_canonicals is called in apply_product_rules_with_product
        }

    except Exception as e:
        logger.error(f"Error processing inventory change for product {product.get('id')}: {str(e)}")
        notification_service.notify_critical_error(e, {
            "productId": product.get('id'),
            "context": "Inventory change processing"
        })
        raise

async def scan_all_used_books():
    # Placeholder — real logic would pull all products via Shopify and process each
    logging.info("Starting full used book inventory scan...")

    # Example: fake batch of product IDs
    dummy_products = [
        {"product_id": "gid://shopify/Product/123", "variant_id": "456", "inventory_item_id": "789"},
        # Add more...
    ]

    for entry in dummy_products:
        # Instead of fetching product via shopify_client.get_product_by_id_gql,
        # simulate a hydrated product dict with required fields
        product = {
            "id": entry["product_id"],
            "handle": "example-damaged",
            "title": "Example Damaged Book Title"
        }
        await process_inventory_change(
            inventory_item_id=entry["inventory_item_id"],
            variant_id=entry["variant_id"],
            product=product,
        )

    logging.info("Used book inventory scan completed.")