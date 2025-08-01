# services/cron_service.py

from services.shopify_client import shopify_client
from services.used_book_manager import process_inventory_change
from services.backup_service import backup_redirects
from services.notification_service import notify_critical_error

import logging
import asyncio

logger = logging.getLogger(__name__)


async def get_all_hurt_books(max_items: int = None, quick_load: bool = False):
    """
    Fetch all hurt books from Shopify based on handle pattern.
    """
    try:
        if quick_load:
            response = await shopify_client.get("products.json", params={"limit": 50})
            products = response.get("products", [])
            hurt_books = [p for p in products if "-hurt-" in p.get("handle", "")]
            logger.info(f"Quick loaded {len(hurt_books)} hurt books")
            return hurt_books

        logger.info(f"Starting hurt books scan (max_items={max_items})")

        products = []
        next_page_token = None
        limit = 250
        request_count = 0
        MAX_REQUESTS = (max_items // limit) + 1 if max_items else 100

        while request_count < MAX_REQUESTS:
            params = {"limit": limit}
            if next_page_token:
                params["page_info"] = next_page_token

            response = await shopify_client.get("products.json", params=params)
            batch = response.get("products", [])
            headers = response.get("headers", {})

            if not batch:
                break

            hurt_books = [p for p in batch if "-hurt-" in p.get("handle", "")]
            products.extend(hurt_books)

            if max_items and len(products) >= max_items:
                logger.info(f"Reached max_items limit ({max_items})")
                return products[:max_items]

            # Parse pagination token
            next_token = None
            link_header = headers.get("Link")
            if link_header:
                parts = link_header.split(",")
                for part in parts:
                    if 'rel="next"' in part:
                        match = part.strip().split("page_info=")
                        if len(match) > 1:
                            next_token = match[1].split("&")[0].replace(">", "")

            if not next_token:
                break

            next_page_token = next_token
            request_count += 1
            await asyncio.sleep(0.1)

        logger.info(f"Completed scan. Found {len(products)} hurt books.")
        return products

    except Exception as e:
        logger.error(f"Error in get_all_hurt_books: {str(e)}")
        raise