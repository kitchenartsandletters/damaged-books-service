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

    async def get_variants_by_inventory_item_id(self, inventory_item_id: int | str) -> list[dict]:
        """
        GET /admin/api/{ver}/variants.json?inventory_item_ids=<csv>
        Returns only variants that truly match inventory_item_id (defensive filter).
        """
        resp = await self.get(
            "variants.json",
            query={
                "inventory_item_ids": str(inventory_item_id),  # CSV string is safest
                "fields": "id,product_id,inventory_item_id",
                "limit": 250,  # raise to reduce chance of paging past target
            }
        )
        body = resp.get("body", {}) if isinstance(resp, dict) else {}
        variants = body.get("variants", [])

        logger.info(f"[ShopifyClient] variants.json returned count={len(variants)} for inventory_item_id={inventory_item_id}")

        # Defensive filter
        inventory_item_id = str(inventory_item_id)
        filtered = [v for v in variants if str(v.get("inventory_item_id")) == inventory_item_id]

        logger.info(
            f"[ShopifyClient] variants returned={len(variants)}, filtered={len(filtered)} "
            f"for inventory_item_id={inventory_item_id}"
        )
        return filtered
    
    async def graph(self, query: str, variables: dict) -> dict:
        """
        Minimal Admin GraphQL client.
        POST /admin/api/{ver}/graphql.json
        """
        url = f"{self.base_url}/graphql.json"
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        }
        payload = {"query": query, "variables": variables}
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=payload, timeout=10.0)
            logger.info(f"[Shopify GQL] POST {url} -> {resp.status_code}")
            resp.raise_for_status()
            return resp.json()

    async def graphql(self, query: str, variables: dict) -> dict:
        return await self.graph(query, variables)

    async def get_variant_product_by_inventory_item(self, inventory_item_id: int | str) -> dict | None:
        """
        GraphQL fallback:
        inventoryItem(id: "gid://shopify/InventoryItem/<id>") { variant { id product { id handle } } }
        Returns {"variant_id": "<num>", "product_id": "<num>", "product_handle": "<str>"} or None.
        """
        gid = f"gid://shopify/InventoryItem/{inventory_item_id}"
        query = """
        query($id: ID!) {
          inventoryItem(id: $id) {
            id
            variant {
              id
              product { id handle }
            }
          }
        }
        """
        data = await self.graph(query, {"id": gid})

        # Basic shape & error-checking
        inv = (data or {}).get("data", {}).get("inventoryItem")
        if not inv or not inv.get("variant"):
            logger.info(f"[ShopifyClient:GQL] No variant for InventoryItem {inventory_item_id}")
            return None

        var_gid = inv["variant"]["id"]                    # e.g. gid://shopify/ProductVariant/1234567890
        prod_gid = inv["variant"]["product"]["id"]        # e.g. gid://shopify/Product/9876543210
        handle   = inv["variant"]["product"].get("handle")

        def gid_to_num(gid_str: str) -> str:
            # last path segment is numeric id
            return gid_str.rsplit("/", 1)[-1]

        out = {
            "variant_id": gid_to_num(var_gid),
            "product_id": gid_to_num(prod_gid),
            "product_handle": handle,
        }
        logger.info(f"[ShopifyClient:GQL] inventory_item_id={inventory_item_id} → variant_id={out['variant_id']} product_id={out['product_id']} handle={handle}")
        return out
    
    async def get_product_by_id(self, product_id: str) -> dict | None:
        resp = await self.get(f"products/{product_id}.json")
        body = resp.get("body", {}) if isinstance(resp, dict) else {}
        return body.get("product")

    async def get_variant_by_id(self, variant_id: str) -> dict | None:
        resp = await self.get(f"variants/{variant_id}.json")
        body = resp.get("body", {}) if isinstance(resp, dict) else {}
        return body.get("variant")

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

    async def get(self, path: str, **kwargs) -> dict:
        # accept either query=... or params=...
        query = kwargs.get("query") or kwargs.get("params")
        return await self._request("GET", path, query=query)

    async def post(self, path: str, **kwargs) -> dict:
        # accept either data=... or json=...
        data = kwargs.get("data") or kwargs.get("json")
        return await self._request("POST", path, data=data)

    async def put(self, path: str, **kwargs) -> dict:
        # accept either data=... or json=...
        data = kwargs.get("data") or kwargs.get("json")
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
    
    async def get_inventory_available(self, inventory_item_id: str, location_id: str | None = None) -> int:
        """
        Returns 'available' for given inventory_item_id at the specified location (if provided).
        Uses REST: GET /inventory_levels.json?inventory_item_ids=&location_ids=
        """
        query = {"inventory_item_ids": str(inventory_item_id)}
        if location_id:
            query["location_ids"] = str(location_id)

        resp = await self.get("inventory_levels.json", query=query)
        body = resp.get("body", {}) if isinstance(resp, dict) else {}
        levels = body.get("inventory_levels", []) or []

        # If multiple locations are returned, prefer the exact location match (if provided)
        if location_id:
            for lvl in levels:
                if str(lvl.get("location_id")) == str(location_id):
                    return int(lvl.get("available") or 0)
        # else: take first / sum? We’ll take first to be explicit and keep code simple.
        return int(levels[0].get("available") or 0) if levels else 0

    async def get_product_metafield(self, product_gid: str, namespace: str, key: str) -> Optional[str]:
        """
        Fetches a product metafield value by product GID, namespace, and key using GraphQL.
        Returns the metafield value as a string (or None if not found).
        """
        query = """
        query($id: ID!, $namespace: String!, $key: String!) {
          product(id: $id) {
            metafield(namespace: $namespace, key: $key) {
              id
              namespace
              key
              value
            }
          }
        }
        """
        variables = {"id": product_gid, "namespace": namespace, "key": key}
        try:
            data = await self.graph(query, variables)
            metafield = (data or {}).get("data", {}).get("product", {}).get("metafield")
            if metafield and metafield.get("value") is not None:
                logger.info(f"[ShopifyClient] Fetched metafield: namespace={namespace}, key={key}, value={metafield['value']}")
                return metafield["value"]
            else:
                logger.info(f"[ShopifyClient] Metafield not found for product_gid={product_gid}, namespace={namespace}, key={key}")
                return None
        except Exception as e:
            logger.error(f"[ShopifyClient] Error fetching metafield: {e}")
            return None

    async def set_product_metafield(self, product_id: str, canonical_handle: str) -> Optional[dict]:
        """
        Sets a product metafield with namespace 'custom', key 'canonical_handle', and the given value.
        Uses the Admin GraphQL API.
        """
        mutation = """
        mutation($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) {
            metafields { id namespace key value }
            userErrors { field message }
          }
        }
        """
        owner_id = f"gid://shopify/Product/{product_id}"
        metafields = [{
            "namespace": "custom",
            "key": "canonical_handle",
            "value": str(canonical_handle),
            "type": "single_line_text_field",
            "ownerId": owner_id
        }]
        variables = {"metafields": metafields}
        try:
            data = await self.graph(mutation, variables)
            result = (data or {}).get("data", {}).get("metafieldsSet", {})
            user_errors = result.get("userErrors", [])
            if user_errors:
                logger.error(f"[ShopifyClient] Error setting metafield: {user_errors}")
                return None
            metafields_ret = result.get("metafields", [])
            logger.info(f"[ShopifyClient] Set metafield for product_id={product_id} canonical_handle={canonical_handle} result={metafields_ret}")
            return metafields_ret
        except Exception as e:
            logger.error(f"[ShopifyClient] Exception setting metafield: {e}")
            return None
        
    
    async def get_product_by_id_gql(self, product_gid: str) -> dict:
        """
        Fetch a Shopify product object by its GraphQL ID.
        Example product_gid: "gid://shopify/Product/1234567890"
        """
        query = """
        query getProduct($id: ID!) {
        product(id: $id) {
            id
            handle
            title
            variants(first: 50) {
            edges {
                node {
                id
                sku
                barcode
                selectedOptions {
                    name
                    value
                }
                inventoryItem {
                    id
                }
                }
            }
            }
        }
        }
        """
        variables = {"id": product_gid}
        resp = await self.graphql(query, variables)
        return resp.get("data", {}).get("product", {})

shopify_client = ShopifyClient()