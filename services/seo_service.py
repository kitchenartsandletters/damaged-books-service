# services/seo_service.py

import logging
from services.shopify_client import shopify_client
from services import redirect_service
import re
from typing import Optional

def normalize_handle(handle: str) -> str:
    # Collapse multiple hyphens to single
    handle = re.sub(r'-{2,}', '-', handle)
    # Strip leading/trailing hyphens
    return handle.strip('-')

async def update_used_book_canonicals(product, canonical_handle):
    """
    Writes the provided canonical handle to the Shopify metafield for the given product.
    Returns a dict with canonical_handle, product_id, and written status.
    Assumes the handle was already resolved by the caller (no extra network reads here).
    Performs exactly one write attempt.
    """
    product_id = product['id']
    logging.info(f"Writing canonical handle '{canonical_handle}' to metafield for product {product_id}")
    written = False
    try:
        await shopify_client.set_product_metafield(product_id, canonical_handle)
        written = True
        logging.info(f"Written canonical handle '{canonical_handle}' to metafield for product {product_id}")
    except Exception as e:
        logging.warning(f"Error writing canonical handle metafield for product {product_id}: {e}")
    return {
        "canonical_handle": canonical_handle,
        "product_id": product_id,
        "written": written
    }

async def resolve_canonical_handle(damaged_handle: str, product: dict | None = None) -> str:
    """
    Resolves the canonical handle for a damaged product handle.
    Resolution order:
      1. Strip '-damaged' from handle.
      2. Check redirect service.
      3. If no redirect and product object provided, trust stripped handle.
      4. If no product object, check Shopify product existence via GraphQL.
      5. Fallback to stripped handle.
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

    stripped = normalize_handle(stripped)
    logger.info(f"Normalized handle to: '{stripped}'")

    # 2. Check redirect service
    try:
        redirect = await redirect_service.find_redirect_by_path(stripped)
        if redirect:
            raw_target = redirect.get('target') or ''
            # Normalize values like "/products/test-book-title" → "test-book-title"
            if raw_target.startswith('/'):
                parts = raw_target.split('/products/', 1)
                normalized = parts[1] if len(parts) == 2 else raw_target.lstrip('/')
            else:
                normalized = raw_target
            if normalized:
                logger.info(f"Canonical handle resolved via redirect: '{normalized}'")
                return normalized
        logger.info(f"No redirect found for handle '{stripped}'")
    except Exception as e:
        logger.warning(f"Error checking redirect for handle '{stripped}': {e}")

    # 3. If product object provided, trust stripped handle without further network calls
    if product is not None:
        logger.info("Product object provided; trusting stripped handle without further checks.")
        return stripped

    # 4. If no product object, confirm existence via GraphQL
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

    # 5. Fallback to stripped
    logger.info(f"Falling back to stripped handle: '{stripped}'")
    return stripped