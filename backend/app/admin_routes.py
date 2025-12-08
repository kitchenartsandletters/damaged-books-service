# backend/app/admin_routes.py
import os
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from services.cron_service import reconcile_damaged_inventory
from services.supabase_client import get_client
from services.damaged_inventory_repo import list_view
from services import product_service
from .schemas import DuplicateCheckRequest, BulkCreateRequest, BulkCreateResult
from services.creation_log_service import log_creation_event
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])
ADMIN_API_TOKEN = os.getenv("VITE_DBS_ADMIN_TOKEN")  # simple shared-secret

def require_admin_token(x_admin_token: str = Header(default="")):
    if not ADMIN_API_TOKEN or x_admin_token != ADMIN_API_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True

class BulkDuplicateCheckRequest(BaseModel):
    entries: list[DuplicateCheckRequest]

@router.get("/damaged-inventory")
def list_damaged_inventory(
    response: Response,
    ok = Depends(require_admin_token),
    limit: int = Query(200, ge=1, le=2000),
    in_stock: bool | None = Query(None),
):
    resp = list_view(limit=limit, in_stock=in_stock)
    data = resp.data or []
    count = len(data)

    # small audit line
    logger.info(f"[Admin] /admin/damaged-inventory -> {count} rows (limit={limit}, in_stock={in_stock})")

    # header for quick CLI checks
    response.headers["X-Result-Count"] = str(count)

    return {"data": data, "meta": {"count": count}}

@router.post("/check-duplicate")
async def check_duplicate(
    payload: DuplicateCheckRequest,
    ok: bool = Depends(require_admin_token),
):
    """
    Bulk Creation Wizard duplicate checker.

    Accepts a proposed canonical + damaged product pair (plus title / ISBN / barcode)
    and returns a structured duplicate/conflict report from product_service.

    The Admin Dashboard will use the result to trigger a conflict-resolution modal:
    - existing canonical?
    - existing damaged?
    - Shopify handle conflicts (including -1 suffix collisions)
    - Supabase inventory rows that would block creation
    """

    result = await product_service.check_damaged_duplicate(
        canonical_handle=payload.canonical_handle
    )

    logger.info(
        "[Admin] /admin/check-duplicate canonical=%s damaged=%s -> status=%s",
        payload.canonical_handle,
        payload.damaged_handle,
        result.get("status"),
    )

    return JSONResponse(result)

@router.post("/bulk-preview")
async def bulk_preview(
    payload: BulkDuplicateCheckRequest,
    ok: bool = Depends(require_admin_token),
):
    """
    Bulk Creation Wizard — preflight scan.

    Accepts a list of (canonical, damaged, isbn, barcode) entries and returns
    a parallel list of conflict reports using product_service.check_damaged_duplicate().

    This is a read-only endpoint: no product creation, no redirects, no inventory writes.
    """

    results = []
    for entry in payload.entries:
        result = await product_service.check_damaged_duplicate(
            canonical_handle=entry.canonical_handle
        )
        results.append(result)

    logger.info(
        "[Admin] /admin/bulk-preview scanned=%s",
        len(results),
    )

    return JSONResponse({"results": results})

@router.post("/bulk-create")
async def bulk_create(
    payload: BulkCreateRequest,
    ok: bool = Depends(require_admin_token),
):
    """
    Damaged Books Service — single canonical → damaged product creator.

    Input: BulkCreateRequest
      - canonical_handle (required)
      - canonical_title (optional hint; canonical title is ultimately pulled from Shopify)
      - isbn / barcode (optional; currently not used for matching)
      - variants: optional list of VariantSeed with:
          * condition: "light" | "moderate" | "heavy"
          * quantity: optional int (reported back as quantity_set; no inventory writes yet)
          * price_override: optional float (fraction OFF; 0.25 == 25% off)
      - dry_run: if True, run checks and return a preview without creating anything in Shopify.

    Behavior:
      - runs check_damaged_duplicate() using canonical_handle + derived damaged handle
      - honors dry_run flag
      - creates one damaged product with 3 condition variants
      - logs to damaged.creation_log via creation_log_service.log_creation_event()
    """

    # Delegate all business logic to the service layer
    result: BulkCreateResult = await product_service.create_damaged_product_with_duplicate_check(payload)

    # Pydantic model → dict for JSONResponse
    return JSONResponse(result.model_dump())

@router.get("/creation-log")
async def get_creation_log(
    ok = Depends(require_admin_token),
    limit: int = Query(100, ge=1, le=500),
):
    """
    Returns the most recent creation log entries.
    """
    from services.creation_log_service import fetch_creation_log

    try:
        rows = await fetch_creation_log(limit=limit)
    except Exception as e:
        logger.warning(f"[Admin] /admin/creation-log failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch creation log")

    return {"data": rows, "meta": {"count": len(rows)}}

@router.post("/reconcile")
async def trigger_reconcile(ok = Depends(require_admin_token)):
    """
    On-demand reconcile. (You can also run this via a worker on a schedule.)
    """
    result = await reconcile_damaged_inventory()
    logger.info(f"[Admin] reconcile raw result: {result}")
    # Expected shape: {"inspected": N, "updated": M, "skipped": K}
    inspected = result.get("inspected", 0)
    updated = result.get("updated", 0)
    skipped  = result.get("skipped", 0)
    logger.info(f"[Admin] reconcile invoked -> inspected={inspected} updated={updated} skipped={skipped}")
    return JSONResponse(result)

@router.get("/reconcile/status")
def get_reconcile_status(ok = Depends(require_admin_token)):
    """
    Returns the latest reconcile run stats (read from damaged.reconcile_log).
    """
    supabase = get_client()

    res = supabase.schema("damaged").from_("reconcile_log") \
        .select("inspected, updated, skipped, note, at") \
        .order("at", desc=True).limit(1).execute()

    data = res.data[0] if res.data else None
    if not data:
        return {"last_run": None}

    return data

@router.get("/logs")
async def logs_link(ok = Depends(require_admin_token)):
    """
    We’re not proxying logs; just return a link hub the UI can use.
    Set GATEWAY_LOGS_URL to your Supabase Log UI or a custom page.
    """
    url = os.getenv("GATEWAY_LOGS_URL", "")
    return {"gateway_logs_url": url}

@router.get("/docs")
async def docs(ok = Depends(require_admin_token)):
    # simple link set; replace with CMS later
    return {
        "links": [
            {"title": "Damaged book policy", "url": "https://…"},
            {"title": "Admin guide", "url": "https://…"},
        ]
    }