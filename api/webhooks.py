# api/webhooks.py

from fastapi import APIRouter, Request, Response, Header
from services.shopify_client import shopify_client
from services.used_book_manager import process_inventory_change
import logging
import json

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
        is_valid = shopify_client.verify_webhook(hmac=x_shopify_hmac_sha256, data=raw_body)

        if not is_valid:
            logger.error("Invalid webhook signature")
            return Response(content="Invalid webhook signature", status_code=401)

        payload = await request.json()
        logger.info(f"Received inventory webhook: {json.dumps(payload)}")

        inventory_item_id = payload.get("inventory_item_id")
        if not inventory_item_id:
            logger.error("No inventory_item_id in webhook payload")
            return Response(content="Missing inventory_item_id", status_code=400)

        logger.info(f"Finding variant for inventory item: {inventory_item_id}")
        variant_response = await shopify_client.get("variants.json", params={"inventory_item_ids": inventory_item_id})
        variants = variant_response.get("variants", [])

        if not variants:
            logger.error(f"No variant found for inventory item {inventory_item_id}")
            return Response(content="No applicable variant found", status_code=200)

        variant = variants[0]
        product_id = variant["product_id"]
        variant_id = variant["id"]

        logger.info(f"Processing inventory change for product: {product_id}, variant: {variant_id}")
        await process_inventory_change(inventory_item_id, variant_id, product_id)

        return Response(content="Webhook processed successfully", status_code=200)

    except Exception as e:
        logger.error(f"Error processing inventory webhook: {str(e)}")
        return Response(content=f"Error processing webhook: {str(e)}", status_code=200)  # Avoid Shopify retries