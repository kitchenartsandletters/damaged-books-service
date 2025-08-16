# services/product_service.py

import logging
from datetime import datetime
from services.shopify_client import shopify_client

logger = logging.getLogger(__name__)

def parse_damaged_handle(handle: str) -> tuple[str, str]:
    import re
    h = (handle or "").lower()
    # Legacy: <base>-(hurt|used|damaged|damage)-(light|moderate|mod|heavy)
    m = re.match(r"^(?P<base>.+)-(?:hurt|used|damaged|damage)-(light|moderate|mod|heavy)$", h)
    if m:
        return m.group("base"), m.group(2)
    # New: <base>-(light|moderate|mod|heavy)-damage
    m = re.match(r"^(?P<base>.+)-(light|moderate|mod|heavy)-damage$", h)
    if m:
        return m.group("base"), m.group(2)
    return handle, None

def is_used_book_handle(handle: str) -> bool:
    base, condition = parse_damaged_handle(handle)
    return condition is not None

def get_new_book_handle_from_used(used_handle: str) -> str:
    base, condition = parse_damaged_handle(used_handle)
    return base

async def _publish_to_online_store(product_id: str) -> None:
    """
    Ensure the product is listed in Online Store.
    POST /product_listings.json
    """
    try:
        payload = {"product_listing": {"product_id": product_id}}
        await shopify_client.post("product_listings.json", data=payload)
    except Exception as e:
        logger.warning(f"Online Store publish failed for product {product_id}: {str(e)}")


async def _unpublish_from_online_store(product_id: str) -> None:
    """
    Ensure the product is removed from Online Store listing.
    DELETE /product_listings/{id}.json
    """
    try:
        await shopify_client.delete(f"product_listings/{product_id}.json")
    except Exception as e:
        # If it wasn't listed, Shopify may 404—treat as benign
        logger.info(f"Online Store unpublish note for product {product_id}: {str(e)}")

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