# services/product_service.py

import logging
from datetime import datetime
from services.shopify_client import shopify_client

logger = logging.getLogger(__name__)


def is_used_book_handle(handle: str) -> bool:
    import re
    pattern = r"-(hurt|used|damaged)-(like-new|very-good|good|acceptable)$"
    return bool(re.search(pattern, (handle or "").lower()))

def get_new_book_handle_from_used(used_handle: str) -> str:
    h = (used_handle or "").lower()
    # Split on the first marker we find (order matters less; all are unique)
    for marker in ("-hurt-", "-used-", "-damaged-"):
        if marker in h:
            return h.split(marker)[0]
    # Fallback: return as-is if no marker present
    return h

# ⬇️ make async and await the client
async def get_product_by_id(product_id: str) -> dict:
    """
    Fetch full Shopify product object by ID.
    """
    try:
        path = f"products/{product_id}.json"
        response = await shopify_client.get(path)
        # response shape: { "status": int, "body": dict, "headers": dict }
        return response.get("body", {}).get("product", {})
    except Exception as e:
        logger.error(f"Error fetching product {product_id}: {str(e)}")
        raise

# ⬇️ make async and await the client
async def set_product_publish_status(product_id: str, should_publish: bool) -> dict:
    """
    Publish or unpublish a Shopify product by setting `published_at`.
    """
    try:
        published_at = datetime.utcnow().isoformat() if should_publish else None
        path = f"products/{product_id}.json"
        payload = {
            "product": {
                "id": product_id,
                "published_at": published_at
            }
        }
        response = await shopify_client.put(path, data=payload)
        return response.get("body", {}).get("product", {})
    except Exception as e:
        action = "publishing" if should_publish else "unpublishing"
        logger.error(f"Error {action} product {product_id}: {str(e)}")
        raise