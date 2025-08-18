# backend/app/admin_routes.py
import os
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Response
from fastapi.responses import JSONResponse
from services.cron_service import reconcile_damaged_inventory
from services.damaged_inventory_repo import list_view
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN")  # simple shared-secret

def require_admin_token(x_admin_token: str = Header(default="")):
    if not ADMIN_API_TOKEN or x_admin_token != ADMIN_API_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True

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

@router.post("/reconcile")
async def trigger_reconcile(ok = Depends(require_admin_token)):
    """
    On-demand reconcile. (You can also run this via a worker on a schedule.)
    """
    result = await reconcile_damaged_inventory()
    # Expected shape: {"inspected": N, "updated": M, "skipped": K}
    inspected = result.get("inspected", 0)
    updated = result.get("updated", 0)
    skipped  = result.get("skipped", 0)
    logger.info(f"[Admin] reconcile invoked -> inspected={inspected} updated={updated} skipped={skipped}")
    return JSONResponse(result)

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