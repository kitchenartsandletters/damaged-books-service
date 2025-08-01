# backend/app/routes.py

from fastapi import APIRouter, Request, HTTPException, status, BackgroundTasks, Query
from fastapi.responses import JSONResponse
import hmac
import hashlib
import base64
import os
from services import used_book_manager
from services import redirect_service
from pydantic import BaseModel
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


class ProductCheckRequest(BaseModel):
    product_id: str
    variant_id: str
    inventory_item_id: str

# Pydantic model for creating a redirect
class RedirectRequest(BaseModel):
    from_path: str
    to_path: str


def verify_shopify_hmac(hmac_header: str, body: bytes, secret: str) -> bool:
    """Verify HMAC signature from Shopify webhook."""
    digest = hmac.new(
        key=secret.encode('utf-8'),
        msg=body,
        digestmod=hashlib.sha256
    ).digest()
    computed_hmac = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed_hmac, hmac_header)


@router.post("/webhooks/inventory-levels")
async def handle_inventory_webhook(request: Request):
    # Get raw request body
    raw_body = await request.body()

    # Get HMAC header
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")
    if not hmac_header:
        raise HTTPException(status_code=400, detail="Missing HMAC header")

    # Get Shopify secret key from environment
    shopify_secret = os.getenv("SHOPIFY_WEBHOOK_SECRET")
    if not shopify_secret:
        raise HTTPException(status_code=500, detail="Missing server secret for webhook validation")

    # Validate HMAC
    if not verify_shopify_hmac(hmac_header, raw_body, shopify_secret):
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    # Parse JSON body
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    inventory_item_id = data.get("inventory_item_id")
    variant_id = data.get("variant_id")
    product_id = data.get("product_id")

    if not all([inventory_item_id, variant_id, product_id]):
        raise HTTPException(status_code=400, detail="Missing required fields in payload")

    try:
        result = await used_book_manager.process_inventory_change(
            inventory_item_id=str(inventory_item_id),
            variant_id=str(variant_id),
            product_id=str(product_id),
        )
        return JSONResponse(status_code=200, content={"status": "success", "result": result})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing inventory update: {str(e)}")

@router.post("/api/products/check")
async def check_product(req: ProductCheckRequest):
    try:
        result = await used_book_manager.process_inventory_change(
            inventory_item_id=req.inventory_item_id,
            variant_id=req.variant_id,
            product_id=req.product_id,
        )
        return JSONResponse(status_code=200, content={"status": "success", "result": result})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Product check failed: {str(e)}")
    
@router.post("/api/products/scan-all")
async def scan_all_products(background_tasks: BackgroundTasks):
    background_tasks.add_task(used_book_manager.scan_all_used_books)
    return JSONResponse(status_code=202, content={"status": "Scan started in background"})

@router.get("/api/redirects")
async def get_redirects():
    try:
        redirects = await redirect_service.get_all_redirects()
        return {"redirects": redirects}
    except Exception as e:
        logger.error(f"Failed to fetch redirects: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch redirects")
    
@router.get("/api/shopify/test")
async def test_shopify_connection():
    try:
        from services.shopify_client import shopify_get

        # Minimal endpoint to confirm API connectivity
        response = await shopify_get("/shop.json")
        shop_info = response.get("shop", {})

        return {
            "success": True,
            "shop": {
                "name": shop_info.get("name"),
                "domain": shop_info.get("domain"),
                "myshopify_domain": shop_info.get("myshopify_domain"),
            },
        }

    except Exception as e:
        logger.error(f"Shopify test connection failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Unable to reach Shopify API")
    
@router.get("/api/products")
async def get_products(page: int = Query(1, gt=0), limit: int = Query(20, le=100)):
    try:
        from services.shopify_client import shopify_get

        endpoint = f"/products.json?limit={limit}&page={page}"
        response = await shopify_get(endpoint)

        return {
            "success": True,
            "products": response.get("products", []),
            "page": page,
            "limit": limit,
        }

    except Exception as e:
        logger.error(f"Failed to fetch products: {str(e)}")
        raise HTTPException(status_code=500, detail="Unable to fetch products")


@router.get("/api/products/{product_id}")
async def get_product(product_id: str):
    try:
        from services.shopify_client import shopify_get

        endpoint = f"/products/{product_id}.json"
        response = await shopify_get(endpoint)

        return {
            "success": True,
            "product": response.get("product", {})
        }

    except Exception as e:
        logger.error(f"Failed to fetch product {product_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Unable to fetch product")


@router.put("/api/products/{product_id}/publish")
async def publish_product(product_id: str):
    try:
        from services.product_service import set_product_publish_status

        await set_product_publish_status(product_id, publish=True)
        return {
            "success": True,
            "message": f"Product {product_id} published"
        }

    except Exception as e:
        logger.error(f"Failed to publish product {product_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to publish product")


@router.put("/api/products/{product_id}/unpublish")
async def unpublish_product(product_id: str):
    try:
        from services.product_service import set_product_publish_status

        await set_product_publish_status(product_id, publish=False)
        return {
            "success": True,
            "message": f"Product {product_id} unpublished"
        }

    except Exception as e:
        logger.error(f"Failed to unpublish product {product_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to unpublish product")
    
@router.post("/api/redirects")
async def create_redirect(redirect_req: RedirectRequest):
    try:
        from services.redirect_service import create_redirect

        redirect = await create_redirect(redirect_req.from_path, redirect_req.to_path)
        if not redirect:
            raise Exception("Failed to create redirect")

        return {
            "success": True,
            "redirect": redirect
        }

    except Exception as e:
        logger.error(f"Redirect creation failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Redirect creation failed")


@router.delete("/api/redirects/{redirect_id}")
async def delete_redirect(redirect_id: str):
    try:
        from services.redirect_service import delete_redirect

        success = await delete_redirect(redirect_id)
        if not success:
            raise Exception("Failed to delete redirect")

        return {
            "success": True,
            "message": f"Redirect {redirect_id} deleted"
        }

    except Exception as e:
        logger.error(f"Redirect deletion failed for ID {redirect_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Redirect deletion failed")


@router.get("/api/redirects/{redirect_id}")
async def get_redirect(redirect_id: str):
    try:
        from services.redirect_service import get_redirect_by_id

        redirect = await get_redirect_by_id(redirect_id)
        if not redirect:
            raise HTTPException(status_code=404, detail="Redirect not found")

        return {
            "success": True,
            "redirect": redirect
        }

    except Exception as e:
        logger.error(f"Failed to fetch redirect {redirect_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch redirect")