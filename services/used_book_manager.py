# services/used_book_manager.py

import logging
from services import product_service, redirect_service, seo_service, inventory_service
from services import notification_service

logger = logging.getLogger(__name__)

async def process_inventory_change(inventory_item_id: str, variant_id: str, product_id: str, available_hint: int | None = None) -> dict:
    try:
        # Optional hint from upstream about current available quantity
        if available_hint is not None:
            try:
                logger.info(
                    f"[Hint] available_hint={available_hint} for inventory_item_id={inventory_item_id}, "
                    f"variant_id={variant_id}, product_id={product_id}"
                )
            except Exception:
                # Logging must not interfere with processing
                pass
        # Get product details
        product = await product_service.get_product_by_id(product_id)
        if not product:
            logger.warning(f"Product {product_id} not found, skipping")
            return

        logger.info(f"[DamagedCheck] handle={product.get('handle')} matched={product_service.is_used_book_handle(product.get('handle',''))}")

        # Check if this is a used book product
        if not product_service.is_used_book_handle(product.get("handle", "")):
            logger.info(f"Product {product_id} is not a used book, skipping")
            return

        # Check stock status
        is_in_stock = await inventory_service.is_variant_in_stock(variant_id, inventory_item_id)
        logger.info(f"Used book {product['handle']} stock status: {'in stock' if is_in_stock else 'out of stock'}")

        # Derive canonical new book handle
        new_book_handle = product_service.get_new_book_handle_from_used(product["handle"])

        # Always set SEO canonical to main product
        canonical_set = await seo_service.update_used_book_canonicals(product, new_book_handle)

        if is_in_stock:
            # Publish used book
            await product_service.set_product_publish_status(product_id, True)
            logger.info(f"Published used book {product['handle']} as it's now in stock")

            # Remove redirect if present
            try:
                existing_redirect = await redirect_service.find_redirect_by_path(product["handle"])
                if existing_redirect:
                    success = await redirect_service.delete_redirect(existing_redirect["id"])
                    if success:
                        logger.info(f"Removed redirect for {product['handle']}")
                    else:
                        logger.warning(f"Failed to remove redirect for {product['handle']}")
                        notification_service.notify("warning", "Redirect Removal Failed",
                            f"Could not remove redirect for {product['handle']}")
            except Exception as e:
                logger.warning(f"Redirect removal error for {product['handle']}: {str(e)}")
                notification_service.notify("warning", "Redirect Operation Failed",
                    f"Error with redirect for {product['handle']}: {str(e)}")

        else:
            # Unpublish and create redirect
            await product_service.set_product_publish_status(product_id, False)
            logger.info(f"Unpublished used book {product['handle']} as it's out of stock")

            try:
                existing = await redirect_service.find_redirect_by_path(product["handle"])
                if existing:
                    logger.info(f"Redirect already exists for {product['handle']} → {new_book_handle}")
                else:
                    created = await redirect_service.create_redirect(product["handle"], new_book_handle)
                    # treat only a dict with an id as success
                    if isinstance(created, dict) and created.get("id"):
                        logger.info(f"Created redirect from {product['handle']} to {new_book_handle}")
                    else:
                        raise RuntimeError("Redirect creation returned empty result")
            except Exception as e:
                logger.warning(f"Redirect operation error for {product['handle']}: {str(e)}")
                notification_service.notify(
                    "warning",
                    "Redirect Operation Failed",
                    f"Error with redirect for {product['handle']}: {str(e)}"
                )

        return {
            "productId": product_id,
            "handle": product["handle"],
            "inStock": is_in_stock,
            "action": "published" if is_in_stock else "unpublished",
            "canonicalSet": bool(canonical_set),
            "availableHint": available_hint,
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