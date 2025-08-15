# services/used_book_manager.py

import logging
from services import product_service, redirect_service, seo_service, inventory_service
from services import notification_service

logger = logging.getLogger(__name__)

async def process_inventory_change(inventory_item_id: str, variant_id: str, product_id: str, available_hint: int | None = None) -> dict:
    try:
        # Get product details
        product = await product_service.get_product_by_id(product_id)
        if not product:
            logger.warning(f"[Inventory] Product {product_id} not found, skipping")
            return {"productId": product_id, "skipped": "product_not_found"}

        handle = (product.get("handle") or "").lower()

        # Damage check (supports -hurt-, -used-, -damaged-)
        is_damaged = product_service.is_used_book_handle(handle)
        logger.info(f"[DamagedCheck] handle={handle} matched={is_damaged}")
        if not is_damaged:
            logger.info(f"[Inventory] Product {product_id} is not a damaged book, skipping")
            return {"productId": product_id, "handle": handle, "skipped": "not_damaged"}

        # Optional hint
        if available_hint is not None:
            logger.info(f"[Hint] available_hint={available_hint} for inventory_item_id={inventory_item_id}, variant_id={variant_id}, product_id={product_id}")

        # Stock status (prefer hint if provided)
        if available_hint is not None:
            is_in_stock = available_hint > 0
        else:
            is_in_stock = await inventory_service.is_variant_in_stock(variant_id, inventory_item_id)
        logger.info(f"[Inventory] Damaged book {handle} stock status: {'in stock' if is_in_stock else 'out of stock'}")

        # Canonical target
        new_book_handle = product_service.get_new_book_handle_from_used(handle)

        # Always set canonicals toward the new book page
        canonical_set = await seo_service.update_used_book_canonicals(product, new_book_handle)

        if is_in_stock:
            # Publish
            updated = await product_service.set_product_publish_status(product_id, True)
            logger.info(f"[Publish] Published damaged book {handle} (id={product_id})")

            # Remove redirect if exists
            existing = await redirect_service.find_redirect_by_path(handle)
            if existing:
                deleted = await redirect_service.delete_redirect(str(existing.get("id")))
                if deleted is not True and deleted not in (200, 204):
                    logger.warning(f"[Redirect] Failed to delete redirect id={existing.get('id')} for {handle}")
                    notification_service.notify(
                        "warning",
                        "Redirect Removal Failed",
                        f"Could not remove redirect for {handle} (id={existing.get('id')})"
                    )
        else:
            # Unpublish
            updated = await product_service.set_product_publish_status(product_id, False)
            logger.info(f"[Publish] Unpublished damaged book {handle} (id={product_id})")

            # Create redirect if missing
            existing = await redirect_service.find_redirect_by_path(handle)
            if not existing:
                created = await redirect_service.create_redirect(handle, new_book_handle)
                if created is None:
                    # Single warning path; avoid duplicate “failed to create” + “operation error”
                    logger.warning(f"[Redirect] Creation returned empty/invalid result for {handle} → {new_book_handle}")
                    notification_service.notify(
                        "warning",
                        "Redirect Creation Failed",
                        f"Could not create redirect from {handle} to {new_book_handle}"
                    )
                else:
                    logger.info(f"[Redirect] Created id={created.get('id')} from {handle} → {new_book_handle}")

        return {
            "productId": product_id,
            "handle": handle,
            "inStock": is_in_stock,
            "action": "published" if is_in_stock else "unpublished",
            "canonicalSet": bool(canonical_set),
        }

    except Exception as e:
        logger.error(f"Error processing inventory change for product {product_id}: {str(e)}")
        notification_service.notify_critical_error(e, {
            "productId": product_id,
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
        await process_inventory_change(
            inventory_item_id=entry["inventory_item_id"],
            variant_id=entry["variant_id"],
            product_id=entry["product_id"],
        )

    logging.info("Used book inventory scan completed.")