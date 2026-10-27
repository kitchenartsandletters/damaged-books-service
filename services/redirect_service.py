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

async def find_redirects_by_path(handle: str) -> list[dict]:
    """
    Return EVERY redirect whose path is /products/{handle}, oldest first.

    Shopify does not stop two redirects with an identical path from being
    created (observed in production: concurrent "check, then create" runs
    produced two identical rows). Callers that only look at the first match
    would leave the others behind, so the dedupe/removal helpers use this.
    """
    path = _path_for_handle(handle)
    resp = await shopify_client.get(REDIRECTS_ENDPOINT, query={"path": path, "limit": 250})
    body = resp.get("body", {}) if isinstance(resp, dict) else {}
    redirects = [r for r in (body.get("redirects") or []) if r.get("path") == path]
    return sorted(redirects, key=lambda r: int(r.get("id") or 0))


class RedirectError(Exception):
    pass


async def _delete_ok(redirect_id) -> bool:
    """delete_redirect, but an already-deleted redirect (404) counts as success."""
    try:
        return await delete_redirect(str(redirect_id))
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 404:
            logger.info(f"[Redirect] id={redirect_id} already gone (404); treating as deleted")
            return True
        raise


async def ensure_single_redirect(from_handle: str, to_handle: str) -> dict:
    """
    Guarantee exactly one redirect from /products/{from_handle}.

    - none   -> create one
    - one    -> leave it
    - many   -> keep the oldest, delete the rest (self-heals races)
    Raises RedirectError if Shopify refuses a create or a delete.
    """
    existing = await find_redirects_by_path(from_handle)
    target = _path_for_handle(to_handle)

    if not existing:
        created = await create_redirect(from_handle, to_handle)
        if created is None:
            raise RedirectError(f"could not create redirect {from_handle} -> {to_handle}")
        return {"created": 1, "deleted": 0, "kept_id": created.get("id")}

    keep, extras = existing[0], existing[1:]
    if keep.get("target") != target:
        # Not changed automatically: an operator may have pointed it elsewhere.
        logger.warning(
            f"[Redirect] Kept redirect id={keep.get('id')} for {from_handle} targets "
            f"{keep.get('target')!r}, expected {target!r}; leaving it unchanged"
        )

    failed = []
    for r in extras:
        if not await _delete_ok(r.get("id")):
            failed.append(r.get("id"))
    if extras:
        logger.warning(
            f"[Redirect] Removed {len(extras) - len(failed)} duplicate redirect(s) for "
            f"{from_handle}; kept id={keep.get('id')}"
        )
    if failed:
        raise RedirectError(f"could not delete duplicate redirect(s) {failed} for {from_handle}")
    return {"created": 0, "deleted": len(extras), "kept_id": keep.get("id")}


async def remove_all_redirects(handle: str) -> int:
    """Delete every redirect from /products/{handle}. Raises RedirectError on any failure."""
    existing = await find_redirects_by_path(handle)
    failed = []
    for r in existing:
        if not await _delete_ok(r.get("id")):
            failed.append(r.get("id"))
    if failed:
        raise RedirectError(f"could not delete redirect(s) {failed} for {handle}")
    return len(existing)


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