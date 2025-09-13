# services/seo_service.py

import logging
from services.shopify_client import shopify_client
from services import redirect_service
import re
from typing import Optional

async def update_used_book_canonicals(product, new_book_handle):
    """
    Determines the canonical handle for a damaged book product.
    Resolution order:
      1. Redirect service (if a redirect exists for the handle)
      2. Handle match (strip '-damaged')
      3. Shopify metafield override
    Returns a dict with resolution method, canonical_handle, product_id, and written status.
    """
    product_id = product['id']
    handle = new_book_handle.strip()
    logging.info(f"Updating canonical tags for product {product_id} with damaged handle '{handle}'")

    # 1. Try redirect service
    redirect = await redirect_service.find_redirect_by_path(handle)
    if redirect and 'target' in redirect and redirect['target']:
        canonical_handle = redirect['target']
        logging.info(f"Canonical handle resolved by redirect: {canonical_handle}")
        # Write back canonical handle to metafield
        written = False
        try:
            await shopify_client.set_product_metafield(product_id, namespace='custom', key='canonical_handle', value=canonical_handle)
            written = True
            logging.info(f"Written canonical handle '{canonical_handle}' to metafield for product {product_id}")
        except Exception as e:
            logging.warning(f"Error writing canonical handle metafield for product {product_id}: {e}")
        return {
            "resolution": "redirect",
            "canonical_handle": canonical_handle,
            "product_id": product_id,
            "written": written
        }

    # 2. Try handle match (strip '-damaged')
    if handle.endswith("-damaged"):
        base_handle = handle[:-len("-damaged")]
        if base_handle:
            logging.info(f"Canonical handle resolved by handle match: {base_handle}")
            # Write back canonical handle to metafield
            written = False
            try:
                await shopify_client.set_product_metafield(product_id, namespace='custom', key='canonical_handle', value=base_handle)
                written = True
                logging.info(f"Written canonical handle '{base_handle}' to metafield for product {product_id}")
            except Exception as e:
                logging.warning(f"Error writing canonical handle metafield for product {product_id}: {e}")
            return {
                "resolution": "handle_match",
                "canonical_handle": base_handle,
                "product_id": product_id,
                "written": written
            }

    # 3. Try metafield override
    try:
        metafield = await shopify_client.get_product_metafield(product_id, 'canonical_handle')
        if metafield and metafield.get('value'):
            canonical_handle = metafield['value']
            logging.info(f"Canonical handle resolved by metafield: {canonical_handle}")
            # Write back canonical handle to metafield
            written = False
            try:
                await shopify_client.set_product_metafield(product_id, namespace='custom', key='canonical_handle', value=canonical_handle)
                written = True
                logging.info(f"Written canonical handle '{canonical_handle}' to metafield for product {product_id}")
            except Exception as e:
                logging.warning(f"Error writing canonical handle metafield for product {product_id}: {e}")
            return {
                "resolution": "metafield",
                "canonical_handle": canonical_handle,
                "product_id": product_id,
                "written": written
            }
    except Exception as e:
        logging.warning(f"Error fetching metafield for product {product_id}: {e}")

    # Failed to resolve
    logging.warning(f"Failed to resolve canonical handle for product {product_id} with handle '{handle}'")
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
      3. Check Shopify product existence.
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

    # 3. Check Shopify product existence
    try:
        endpoint = f"/products/{stripped}.json"
        response = await shopify_client.get(endpoint)
        if response and response.get("product"):
            logger.info(f"Canonical handle resolved via Shopify product: '{stripped}'")
            return stripped
        else:
            logger.info(f"No Shopify product found for handle '{stripped}'")
    except Exception as e:
        logger.warning(f"Error fetching Shopify product for handle '{stripped}': {e}")

    # 4. Fallback to stripped
    logger.info(f"Falling back to stripped handle: '{stripped}'")
    return stripped