# services/seo_service.py

import logging
from services.shopify_client import shopify_client
from services import redirect_service

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