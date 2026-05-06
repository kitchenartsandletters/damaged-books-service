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
ADMIN_API_TOKEN = os.getenv("VITE_DBS_ADMIN_TOKEN")


def require_admin_token(x_admin_token: str = Header(default="")):
    if not ADMIN_API_TOKEN or x_admin_token != ADMIN_API_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True


class BulkDuplicateCheckRequest(BaseModel):
    entries: list[DuplicateCheckRequest]


@router.get("/damaged-inventory")
def list_damaged_inventory(
    response: Response,
    ok=Depends(require_admin_token),
    limit: int = Query(200, ge=1, le=2000),
    in_stock: bool | None = Query(None),
):
    resp = list_view(limit=limit, in_stock=in_stock)
    data = resp.data or []
    count = len(data)
    logger.info(f"[Admin] /admin/damaged-inventory -> {count} rows (limit={limit}, in_stock={in_stock})")
    response.headers["X-Result-Count"] = str(count)
    return {"data": data, "meta": {"count": count}}


@router.post("/check-duplicate")
async def check_duplicate(
    payload: DuplicateCheckRequest,
    ok: bool = Depends(require_admin_token),
):
    """
    Bulk Creation Wizard duplicate checker.
    damaged_handle is auto-derived from canonical_handle inside check_damaged_duplicate.
    """
    result = await product_service.check_damaged_duplicate(
        canonical_handle=payload.canonical_handle,
        # damaged_handle is optional — defaults to canonical_handle + "-damaged"
    )

    logger.info(
        "[Admin] /admin/check-duplicate canonical=%s -> status=%s",
        payload.canonical_handle,
        result.get("status"),
    )

    return JSONResponse(result)


@router.post("/bulk-preview")
async def bulk_preview(
    payload: BulkDuplicateCheckRequest,
    ok: bool = Depends(require_admin_token),
):
    """
    Bulk Creation Wizard — preflight scan (read-only).
    """
    results = []
    for entry in payload.entries:
        result = await product_service.check_damaged_duplicate(
            canonical_handle=entry.canonical_handle,
        )
        results.append(result)

    logger.info("[Admin] /admin/bulk-preview scanned=%s", len(results))
    return JSONResponse({"results": results})


@router.post("/bulk-create/preview")
async def bulk_create_preview(
    payload: BulkCreateRequest,
    ok: bool = Depends(require_admin_token),
):
    """
    Damaged Books Service — Bulk Create PREVIEW ONLY.
    No Shopify writes. No Supabase writes.
    """
    if not payload.inputs:
        raise HTTPException(status_code=422, detail="inputs[] is required for bulk preview")

    if payload.canonical_handle:
        raise HTTPException(status_code=422, detail="canonical_handle is not allowed for bulk preview")

    if not payload.inventory:
        raise HTTPException(status_code=422, detail="inventory is required for bulk preview")

    try:
        resolved = await product_service.resolve_bulk_inputs(payload.inputs)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("[Admin] bulk-create preview resolution failed")
        raise HTTPException(status_code=500, detail="Failed to resolve inputs")

    try:
        preview_rows = []
        for r in resolved:
            rows = product_service.compute_damaged_variant_preview(
                canonical_product_id=r["product_id"],
                canonical_handle=r["handle"],
                canonical_variant=r["variant"],
                inventory_seed=payload.inventory,
            )
            preview_rows.extend(rows)
    except Exception:
        logger.exception("[Admin] bulk-create preview build failed")
        raise HTTPException(status_code=500, detail="Failed to build preview")

    logger.info("[Admin] /admin/bulk-create/preview rows=%s", len(preview_rows))
    return JSONResponse({
        "ok": True,
        "preview": preview_rows,
        "meta": {"count": len(preview_rows)},
    })


@router.post("/bulk-create")
async def bulk_create(
    payload: BulkCreateConfirmRequest,
    ok: bool = Depends(require_admin_token),
):
    """
    Damaged Books Service — Bulk Create CONFIRM.

    Executes preview-derived confirm payloads.
    Each item is a BulkCreateConfirmItem with:
      canonical_product_id, canonical_handle, condition_key, inventory (int).

    Routing logic (inside product_service):
      - Damaged product already exists → update inventory quantities only
      - Damaged product not found     → create fresh with weight + collection membership
    """
    if not payload.items:
        raise HTTPException(status_code=422, detail="items[] is required")

    # Validate required fields on BulkCreateConfirmItem
    for idx, item in enumerate(payload.items):
        if not hasattr(item, "canonical_handle") or not item.canonical_handle:
            logger.warning(f"[Admin] bulk-create confirm item at index {idx} missing canonical_handle")
            raise HTTPException(
                status_code=422,
                detail=f"Item at index {idx} missing required field: canonical_handle",
            )
        if not hasattr(item, "condition_key") or not item.condition_key:
            logger.warning(f"[Admin] bulk-create confirm item at index {idx} missing condition_key")
            raise HTTPException(
                status_code=422,
                detail=f"Item at index {idx} missing required field: condition_key",
            )

    try:
        result = await product_service.create_damaged_from_preview_items(payload.items)
    except Exception as e:
        logger.exception("[Admin] /admin/bulk-create failed")
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse(result)


@router.get("/creation-log")
async def get_creation_log(
    ok=Depends(require_admin_token),
    limit: int = Query(100, ge=1, le=500),
):
    from services.creation_log_service import fetch_creation_log
    try:
        rows = await fetch_creation_log(limit=limit)
    except Exception as e:
        logger.warning(f"[Admin] /admin/creation-log failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch creation log")
    return {"data": rows, "meta": {"count": len(rows)}}


@router.post("/reconcile")
async def trigger_reconcile(ok=Depends(require_admin_token)):
    result = await reconcile_damaged_inventory()
    logger.info(f"[Admin] reconcile raw result: {result}")
    inspected = result.get("inspected", 0)
    updated = result.get("updated", 0)
    skipped = result.get("skipped", 0)
    logger.info(f"[Admin] reconcile invoked -> inspected={inspected} updated={updated} skipped={skipped}")
    return JSONResponse(result)


@router.get("/reconcile/status")
def get_reconcile_status(ok=Depends(require_admin_token)):
    supabase = get_client()
    res = (
        supabase.schema("damaged")
        .from_("reconcile_log")
        .select("inspected, updated, skipped, note, at")
        .order("at", desc=True)
        .limit(1)
        .execute()
    )
    data = res.data[0] if res.data else None
    if not data:
        return {"last_run": None}
    return data


@router.get("/logs")
async def logs_link(ok=Depends(require_admin_token)):
    url = os.getenv("GATEWAY_LOGS_URL", "")
    return {"gateway_logs_url": url}


@router.get("/docs")
async def docs(ok=Depends(require_admin_token)):
    return {
        "links": [
            {"title": "Damaged book policy", "url": "https://…"},
            {"title": "Admin guide", "url": "https://…"},
        ]
    }