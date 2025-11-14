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
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])
ADMIN_API_TOKEN = os.getenv("VITE_DBS_ADMIN_TOKEN")  # simple shared-secret

def require_admin_token(x_admin_token: str = Header(default="")):
    if not ADMIN_API_TOKEN or x_admin_token != ADMIN_API_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True

class DuplicateCheckRequest(BaseModel):
    canonical_handle: Optional[str] = None
    canonical_title: Optional[str] = None
    damaged_handle: Optional[str] = None
    damaged_title: Optional[str] = None
    isbn: Optional[str] = None
    barcode: Optional[str] = None

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
        canonical_handle=payload.canonical_handle,
        canonical_title=payload.canonical_title,
        damaged_handle=payload.damaged_handle,
        damaged_title=payload.damaged_title,
        isbn=payload.isbn,
        barcode=payload.barcode,
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
            canonical_handle=entry.canonical_handle,
            canonical_title=entry.canonical_title,
            damaged_handle=entry.damaged_handle,
            damaged_title=entry.damaged_title,
            isbn=entry.isbn,
            barcode=entry.barcode,
        )
        results.append(result)

    logger.info(
        "[Admin] /admin/bulk-preview scanned=%s",
        len(results),
    )

    return JSONResponse({"results": results})

@router.post("/bulk-create")
async def bulk_create(
    payload: BulkDuplicateCheckRequest,
    ok: bool = Depends(require_admin_token),
):
    """
    Bulk Creation Wizard — execute creation of canonical + damaged products.

    Preconditions:
      - The Dashboard must have run /bulk-preview and shown operators all conflicts.
      - Only entries whose preview.status == 'ok_to_create' should be sent here.

    Behavior:
      - Phase 1: re-run check_damaged_duplicate() for every entry.
        * If ANY entry is not ok_to_create → abort the entire batch with 409.
      - Phase 2: create all pairs via product_service.create_damaged_pair().
        * No redirects.
        * No Supabase inventory writes.
    """

    # -------------------------------
    # Phase 1 — global duplicate guard
    # -------------------------------
    prechecked: list[dict] = []
    has_conflicts = False

    for entry in payload.entries:
        precheck = await product_service.check_damaged_duplicate(
            canonical_handle=entry.canonical_handle,
            canonical_title=entry.canonical_title,
            damaged_handle=entry.damaged_handle,
            damaged_title=entry.damaged_title,
            isbn=entry.isbn,
            barcode=entry.barcode,
        )
        prechecked.append({"entry": entry, "precheck": precheck})

        if precheck.get("status") != "ok_to_create":
            has_conflicts = True

    if has_conflicts:
        conflicts = []
        for item in prechecked:
            status = item["precheck"].get("status")
            if status != "ok_to_create":
                conflicts.append(
                    {
                        "requested": item["entry"].model_dump(),
                        "precheck": item["precheck"],
                    }
                )

        logger.warning(
            "[Admin] /admin/bulk-create ABORTED: %s conflicted entries",
            len(conflicts),
        )

        # Hard guard: nothing is created if any entry is not ok_to_create
        return JSONResponse(
            status_code=409,
            content={
                "status": "conflict",
                "message": "One or more entries are not ok_to_create; no products were created.",
                "conflicts": conflicts,
            },
        )

    # -------------------------------
    # Phase 2 — safe to create all pairs
    # -------------------------------
    results = []

    for item in prechecked:
        entry: DuplicateCheckRequest = item["entry"]

        try:
            created = await product_service.create_damaged_pair(
                canonical_title=entry.canonical_title,
                canonical_handle=entry.canonical_handle,
                damaged_title=entry.damaged_title,
                damaged_handle=entry.damaged_handle,
                isbn=entry.isbn,
                barcode=entry.barcode,
            )

            results.append(
                {
                    "requested": entry.model_dump(),
                    "status": "created",
                    "created": created,
                }
            )

        except Exception as e:
            logger.warning(
                "[Admin] bulk-create failed for canonical=%s damaged=%s err=%s",
                entry.canonical_handle,
                entry.damaged_handle,
                str(e),
            )
            results.append(
                {
                    "requested": entry.model_dump(),
                    "status": "error",
                    "error": str(e),
                }
            )

    logger.info(
        "[Admin] /admin/bulk-create -> processed=%s",
        len(results),
    )

    return JSONResponse({"results": results})

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