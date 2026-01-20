import os
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from services.cron_service import reconcile_damaged_inventory
from services.supabase_client import get_client
from services.damaged_inventory_repo import list_view
from services import product_service
from .schemas import DuplicateCheckRequest, BulkCreateRequest, BulkCreateResult, BulkCreateConfirmRequest
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

@router.post("/bulk-create/preview")
async def bulk_create_preview(
    payload: BulkCreateRequest,
    ok: bool = Depends(require_admin_token),
):
    """
    Damaged Books Service — Bulk Create PREVIEW ONLY.

    Accepts:
      - inputs[] + inventory

    Returns:
      - preview rows only
      - no Shopify writes
      - no Supabase writes
    """

    # ----------------------------
    # HARD VALIDATION
    # ----------------------------
    if not payload.inputs:
        raise HTTPException(
            status_code=422,
            detail="inputs[] is required for bulk preview"
        )

    if payload.canonical_handle:
        raise HTTPException(
            status_code=422,
            detail="canonical_handle is not allowed for bulk preview"
        )

    if not payload.inventory:
        raise HTTPException(
            status_code=422,
            detail="inventory is required for bulk preview"
        )

    # ----------------------------
    # RESOLUTION
    # ----------------------------
    try:
        resolved = await product_service.resolve_bulk_inputs(payload.inputs)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("[Admin] bulk-create preview resolution failed")
        raise HTTPException(status_code=500, detail="Failed to resolve inputs")

    # ----------------------------
    # PREVIEW CONSTRUCTION (PURE)
    # ----------------------------
    try:
        preview_rows = product_service.compute_damaged_variant_preview(
            resolved=resolved,
            inventory=payload.inventory,
        )
    except Exception as e:
        logger.exception("[Admin] bulk-create preview build failed")
        raise HTTPException(status_code=500, detail="Failed to build preview")

    logger.info(
        "[Admin] /admin/bulk-create/preview rows=%s",
        len(preview_rows),
    )

    return JSONResponse(
        {
            "ok": True,
            "preview": preview_rows,
            "meta": {"count": len(preview_rows)},
        }
    )

@router.post("/bulk-create")
async def bulk_create(
    payload: BulkCreateConfirmRequest,
    ok: bool = Depends(require_admin_token),
):
    """
    Damaged Books Service — Bulk Create CONFIRM.

    Executes preview-derived confirm payloads only.
    """

    if not payload.items:
        raise HTTPException(
            status_code=422,
            detail="items[] is required"
        )

    # Additional per-item validation/logging
    for idx, item in enumerate(payload.items):
        if not hasattr(item, "canonical_handle") or not hasattr(item, "damaged_handle"):
            logger.warning(f"[Admin] bulk-create confirm item at index {idx} missing required fields")
            raise HTTPException(status_code=422, detail=f"Item at index {idx} missing required fields")

    try:
        # Fix: use correct function name create_damaged_from_preview_items
        result = await product_service.create_damaged_from_preview_items(payload.items)
    except Exception as e:
        logger.exception("[Admin] /admin/bulk-create failed")
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse(result)

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