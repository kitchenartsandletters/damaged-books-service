# services/seo_service.py

import logging
from services.shopify_client import shopify_client
from services import redirect_service
import re
from typing import Optional

async def update_used_book_canonicals(product, new_book_handle):
    """
    Determines the canonical handle for a damaged book product by resolving it and writing to metafield.
    Returns a dict with resolution method, canonical_handle, product_id, and written status.
    """
    product_id = product['id']
    handle = new_book_handle.strip()
    logging.info(f"Updating canonical tags for product {product_id} with damaged handle '{handle}'")

    try:
        canonical_handle = await resolve_canonical_handle(handle, product)
        written = False
        try:
            await shopify_client.set_product_metafield(product_id, namespace='custom', key='canonical_handle', value=canonical_handle)
            written = True
            logging.info(f"Written canonical handle '{canonical_handle}' to metafield for product {product_id}")
        except Exception as e:
            logging.warning(f"Error writing canonical handle metafield for product {product_id}: {e}")
        return {
            "resolution": "resolved",
            "canonical_handle": canonical_handle,
            "product_id": product_id,
            "written": written
        }
    except Exception as e:
        logging.warning(f"Failed to resolve and write canonical handle for product {product_id} with handle '{handle}': {e}")
        return {
            "resolution": "failed",
            "canonical_handle": None,
            "product_id": product_id,
            "written": False
        }

async def resolve_canonical_handle(damaged_handle: str, product: dict | None = None) -> str:
    """
    Resolves the canonical handle for a damaged product handle.
    Resolution order:
      1. Strip '-damaged' from handle.
      2. Check redirect service.
      3. Check Shopify product existence via GraphQL.
      4. Fallback to stripped handle.
    Returns the canonical handle string.
    """
    logger = logging.getLogger("seo_service")
    logger.info(f"Resolving canonical handle for damaged handle: '{damaged_handle}'")

    # 1. Strip '-damaged'
    if damaged_handle.endswith("-damaged"):
        stripped = damaged_handle[:-len("-damaged")]
        logger.info(f"Stripped '-damaged' from handle: '{stripped}'")
    else:
        stripped = damaged_handle
        logger.info(f"No '-damaged' suffix found. Using handle as-is: '{stripped}'")

    # 2. Check redirect service
    try:
        redirect = await redirect_service.find_redirect_by_path(stripped)
        if redirect and 'target' in redirect and redirect['target']:
            logger.info(f"Canonical handle resolved via redirect: '{redirect['target']}'")
            return redirect['target']
        else:
            logger.info(f"No redirect found for handle '{stripped}'")
    except Exception as e:
        logger.warning(f"Error checking redirect for handle '{stripped}': {e}")

    # 3. Check Shopify product existence via GraphQL
    try:
        query = """
        query productByHandle($handle: String!) {
          productByHandle(handle: $handle) { id handle title }
        }
        """
        variables = {"handle": stripped}
        response = await shopify_client.graphql(query, variables)
        product_data = response.get("data", {}).get("productByHandle")
        if product_data:
            logger.info(f"Canonical handle resolved via Shopify product (GraphQL): '{stripped}'")
            return stripped
        else:
            logger.info(f"No Shopify product found for handle '{stripped}' via GraphQL")
    except Exception as e:
        logger.warning(f"Error fetching Shopify product for handle '{stripped}' via GraphQL: {e}")

    # 4. Fallback to stripped
    logger.info(f"Falling back to stripped handle: '{stripped}'")
    return stripped