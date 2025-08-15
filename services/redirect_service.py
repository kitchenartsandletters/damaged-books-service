# services/redirect_service.py

import logging
from services.shopify_client import shopify_client

logger = logging.getLogger(__name__)

REDIRECTS_ENDPOINT = "redirects.json"

def _path_for_handle(handle: str) -> str:
    # store paths are always prefixed with /products/ in this workflow
    return f"/products/{handle}"
    
async def get_all_redirects():
    resp = await shopify_client.get(REDIRECTS_ENDPOINT)
    body = resp.get("body", {}) if isinstance(resp, dict) else {}
    return body.get("redirects", [])

async def get_redirect_by_id(redirect_id: str):
    resp = await shopify_client.get(f"redirects/{redirect_id}.json")
    body = resp.get("body", {}) if isinstance(resp, dict) else {}
    return body.get("redirect")

async def find_redirect_by_path(handle: str):
    path = _path_for_handle(handle)
    resp = await shopify_client.get(REDIRECTS_ENDPOINT, query={"path": path})
    body = resp.get("body", {}) if isinstance(resp, dict) else {}
    redirects = body.get("redirects", [])
    if redirects:
        logger.info(f"[Redirect] Found existing redirect for path: {handle} -> {redirects[0].get('target')}")
        return redirects[0]
    logger.info(f"[Redirect] No redirect found for path: {handle}")
    return None

async def create_redirect(from_handle: str, to_handle: str):
    payload = {
        "redirect": {
            "path": _path_for_handle(from_handle),
            "target": _path_for_handle(to_handle),
        }
    }
    resp = await shopify_client.post(REDIRECTS_ENDPOINT, data=payload)
    body = resp.get("body", {}) if isinstance(resp, dict) else {}
    redirect = body.get("redirect")

    # Normalize: require an id to consider success
    if isinstance(redirect, dict) and redirect.get("id"):
        logger.info(f"[Redirect] Created redirect {redirect['id']} from {from_handle} → {to_handle}")
        return redirect

    logger.warning(f"[Redirect] Create returned unexpected shape: {body}")
    return None


async def delete_redirect(redirect_id: str) -> bool:
    resp = await shopify_client.delete(f"redirects/{redirect_id}.json")
    # Shopify returns 200 with an empty body on success for DELETE
    ok = bool(resp) and int(resp.get("status", 0)) in (200, 204)
    if ok:
        logger.info(f"[Redirect] Deleted redirect id={redirect_id}")
    else:
        logger.warning(f"[Redirect] Delete may have failed for id={redirect_id}: {resp}")
    return ok