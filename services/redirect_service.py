# services/redirect_service.py

import logging
from services.shopify_client import shopify_client

logger = logging.getLogger(__name__)

REDIRECTS_ENDPOINT = "/redirects"

async def find_redirect_by_path(path: str) -> dict | None:
    """
    Look up an existing redirect by Shopify product path.
    Returns the first matching redirect, or None.
    """
    try:
        response = await shopify_client.get("redirects.json", query={"path": f"/products/{path}"})
        redirects = response.get("redirects", [])
        
        if not redirects:
            logger.info(f"No redirect found for path: {path}")
            return None

        return redirects[0]

    except Exception as e:
        logger.error(f"Error finding redirect for path {path}: {str(e)}")
        return None


async def create_redirect(used_book_path: str, target_path: str) -> dict | None:
    """
    Create a 302 redirect from the used book path to the canonical target.
    """
    try:
        payload = {
            "redirect": {
                "path": f"/products/{used_book_path}",
                "target": f"/products/{target_path}",
                "redirect_type": "302"
            }
        }
        response = await shopify_client.post("redirects.json", json=payload)
        logger.info(f"Created redirect from {used_book_path} to {target_path}")
        return response.get("redirect")

    except Exception as e:
        logger.error(f"Error creating redirect from {used_book_path} to {target_path}: {str(e)}")
        return None


async def delete_redirect(redirect_id: str) -> bool:
    """
    Deletes a Shopify redirect by ID.
    """
    try:
        if not redirect_id:
            logger.warning("Attempted to delete redirect with null or undefined ID")
            return False

        await shopify_client.delete(f"redirects/{redirect_id}.json")
        logger.info(f"Deleted redirect {redirect_id}")
        return True

    except Exception as e:
        logger.error(f"Error deleting redirect {redirect_id}: {str(e)}")
        return False
    
async def get_all_redirects() -> list:
    # This function will call Shopify API: GET /admin/api/2023-07/redirects.json
    # Example implementation placeholder

    endpoint = "/redirects.json"
    result = await shopify_client.get(endpoint)
    return result.get("redirects", [])

async def get_redirect_by_id(redirect_id: str) -> dict:
    try:
        response = await shopify_client.get(f"{REDIRECTS_ENDPOINT}/{redirect_id}")
        return response.get("redirect") if response else None
    except Exception as e:
        logger.error(f"Failed to fetch redirect with ID {redirect_id}: {str(e)}")
        return None