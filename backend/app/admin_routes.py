# backend/app/admin_routes.py
import os
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from fastapi.responses import JSONResponse
from services.cron_service import reconcile_damaged_inventory
from services.damaged_inventory_repo import damaged_inventory_repo  # Add this import

router = APIRouter(prefix="/admin", tags=["Admin"])
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN")  # simple shared-secret

def require_admin_token(x_admin_token: str = Header(default="")):
    if not ADMIN_API_TOKEN or x_admin_token != ADMIN_API_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True

@router.get("/damaged-inventory")
async def list_damaged_inventory(
    ok = Depends(require_admin_token),
    limit: int = Query(200, ge=1, le=2000),
    in_stock: Optional[bool] = Query(None)
):
    try:
        res = damaged_inventory_repo.list_view(limit=limit, in_stock=in_stock)
        rows = getattr(res, "data", None) or []
        return {"data": rows, "meta": {"count": len(rows)}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/reconcile")
async def trigger_reconcile(ok = Depends(require_admin_token)):
    try:
        result = await reconcile_damaged_inventory()
        return JSONResponse(content=result or {"ok": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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