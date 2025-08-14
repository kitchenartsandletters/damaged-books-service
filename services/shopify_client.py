# services/shopify_client.py

import os
import time
import json
import logging
import hmac
import hashlib
import base64
from typing import Optional, Dict, Any
import httpx
import asyncio
from urllib.parse import urlencode
from config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
SHOP_URL = settings.SHOP_URL
SHOPIFY_ACCESS_TOKEN = settings.SHOPIFY_ACCESS_TOKEN
SHOPIFY_API_SECRET = settings.SHOPIFY_API_SECRET

API_VERSION = "2025-01"

BASE_URL = f"https://{SHOP_URL}/admin/api/{API_VERSION}"

if not SHOP_URL or not SHOPIFY_ACCESS_TOKEN:
    raise ValueError("Missing required Shopify config: SHOP_URL or SHOPIFY_ACCESS_TOKEN")

class ShopifyClient:
    def __init__(self):
        self.base_url = f"https://{SHOP_URL}/admin/api/{API_VERSION}"
        self.headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN
        }

    async def get_variants_by_inventory_item_id(self, inventory_item_id: int) -> list:
        """
        Convenience helper for:
        GET /admin/api/{ver}/variants.json?inventory_item_ids=<id>
        Returns the 'variants' array ([] if none).
        """
        resp = await self.get(
            "variants.json",
            query={"inventory_item_ids": inventory_item_id}
        )
        # resp shape per _request(): { "status": int, "body": dict, "headers": dict }
        body = resp.get("body", {}) if isinstance(resp, dict) else {}
        return body.get("variants", [])

    def _build_url(self, path: str, query: Optional[Dict[str, Any]] = None) -> str:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url += f"?{urlencode(query)}"
        return url

    async def _request(self, method: str, path: str, data: Optional[dict] = None, query: Optional[dict] = None) -> dict:
        url = self._build_url(path, query)
        async with httpx.AsyncClient() as client:
            for attempt in range(3):  # max 3 retries
                try:
                    response = await client.request(
                        method=method.upper(),
                        url=url,
                        headers=self.headers,
                        json=data if method in ["POST", "PUT"] else None,
                        timeout=10.0
                    )

                    logger.info(f"[Shopify] {method.upper()} {url} -> {response.status_code}")

                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", "1"))
                        logger.warning(f"Rate limited. Retrying after {retry_after}s...")
                        await asyncio.sleep(retry_after)
                        continue

                    response.raise_for_status()
                    return {
                        "status": response.status_code,
                        "body": response.json(),
                        "headers": dict(response.headers)
                    }
                except httpx.RequestError as e:
                    logger.error(f"Request error: {e}")
                except httpx.HTTPStatusError as e:
                    logger.error(f"HTTP status error: {e.response.text}")
                    raise

            raise Exception("Exceeded retry attempts due to rate limiting or request failure")

    async def get(self, path: str, query: Optional[dict] = None) -> dict:
        return await self._request("GET", path, query=query)

    async def post(self, path: str, data: dict) -> dict:
        return await self._request("POST", path, data=data)

    async def put(self, path: str, data: dict) -> dict:
        return await self._request("PUT", path, data=data)

    async def delete(self, path: str) -> dict:
        return await self._request("DELETE", path)

    def verify_webhook(self, hmac_header: str, data: bytes) -> bool:
        digest = hmac.new(
            SHOPIFY_API_SECRET.encode("utf-8"),
            msg=data,
            digestmod=hashlib.sha256
        ).digest()
        computed_hmac = base64.b64encode(digest).decode()
        return hmac.compare_digest(computed_hmac, hmac_header)


shopify_client = ShopifyClient()