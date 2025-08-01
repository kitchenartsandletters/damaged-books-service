# api/system.py

import logging
from fastapi import APIRouter, HTTPException
from services.shopify_client import shopify_client

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/health")
async def health_check():
    return {"status": "ok"}

@router.get("/api/shopify/test")
async def test_shopify_connection():
    try:

        # Minimal endpoint to confirm API connectivity
        response = await shopify_client.get("/shop.json")
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