# services/used_book_manager.py

import logging
from services import product_service, redirect_service, seo_service, inventory_service
from utils import notification_service

logger = logging.getLogger(__name__)


async def process_inventory_change(inventory_item_id: str, variant_id: str, product_id: str) -> dict:
    try:
        # Get product details
        product = await product_service.get_product_by_id(product_id)
        if not product:
            logger.warning(f"Product {product_id} not found, skipping")
            return

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
                existing_redirect = await redirect_service.find_redirect_by_path(product["handle"])
                if not existing_redirect:
                    redirect = await redirect_service.create_redirect(product["handle"], new_book_handle)
                    if redirect:
                        logger.info(f"Created redirect from {product['handle']} to {new_book_handle}")
                    else:
                        logger.warning(f"Failed to create redirect from {product['handle']} to {new_book_handle}")
                        notification_service.notify("warning", "Redirect Creation Failed",
                            f"Could not create redirect from {product['handle']} to {new_book_handle}")
            except Exception as e:
                logger.warning(f"Redirect creation error for {product['handle']}: {str(e)}")
                notification_service.notify("warning", "Redirect Operation Failed",
                    f"Error with redirect for {product['handle']}: {str(e)}")

        return {
            "productId": product_id,
            "handle": product["handle"],
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