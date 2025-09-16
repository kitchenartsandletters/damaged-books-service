# api/webhooks.py

from fastapi import APIRouter, Request, Header
from fastapi.responses import JSONResponse
from services.shopify_client import shopify_client
from services.used_book_manager import process_inventory_change
import logging
import json
import os

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/webhooks/inventory-levels")
async def handle_inventory_level_update(
    request: Request,
    x_shopify_hmac_sha256: str = Header(None)
):
    """
    Handle Shopify webhook for inventory-level updates.
    """

    try:
        raw_body = await request.body()
        is_valid = await shopify_client.verify_webhook(hmac=x_shopify_hmac_sha256, data=raw_body)

        if not is_valid:
            logger.error("Invalid webhook signature")
            return JSONResponse(content={"error": "Invalid webhook signature"}, status_code=401)

        payload = await request.json()
        logger.info(f"Received inventory webhook: {json.dumps(payload)}")

        inventory_item_id = payload.get("inventory_item_id")
        if not inventory_item_id:
            logger.error("No inventory_item_id in webhook payload")
            return JSONResponse(content={"error": "Missing inventory_item_id"}, status_code=400)

        logger.info(f"Processing inventory change for inventory_item_id={inventory_item_id}")
        
        variant_info = await shopify_client.get_variant_product_by_inventory_item(inventory_item_id)
        product_id = variant_info.get("product_id")
        variant_id = variant_info.get("variant_id")

        product = await shopify_client.get_product_by_id_gql(product_id)
        await process_inventory_change(
            inventory_item_id=inventory_item_id,
            variant_id=variant_id,
            product=product,
        )

        return JSONResponse(content={"status": "ok"})

    except Exception as e:
        logger.error(f"Error processing inventory webhook: {str(e)}")
        return JSONResponse(content={"status": "ok", "note": f"error: {str(e)}"})  # Avoid Shopify retries