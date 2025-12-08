# services/creation_log_service.py

from typing import Optional, List, Dict, Any
import logging

from services.supabase_client import get_client
from backend.app.schemas import BulkCreateRequest, BulkCreateResult, CreatedVariantInfo

logger = logging.getLogger(__name__)


def _serialize_variants(variants: List[CreatedVariantInfo]) -> list[dict[str, Any]]:
    """
    Convert CreatedVariantInfo Pydantic models to a plain JSON-serializable list.
    """
    serialized: list[dict[str, Any]] = []
    for v in variants or []:
        serialized.append(
            {
                "condition": v.condition,
                "variant_id": v.variant_id,
                "quantity_set": v.quantity_set,
                "price": v.price,
                "sku": v.sku,
                "barcode": v.barcode,
                "inventory_management": v.inventory_management,
                "inventory_policy": v.inventory_policy,
            }
        )
    return serialized


async def log_creation_event(
    request: BulkCreateRequest,
    result: BulkCreateResult,
    operator: Optional[str] = None,
) -> None:
    """
    Best-effort audit log writer for damaged product creation attempts.

    - Logs ALL runs (success, dry-run, error) per Option A.
    - Never raises; errors are logged and swallowed.
    """

    supabase = get_client()

    try:
        variants_payload = _serialize_variants(result.variants)

        message: Optional[str] = None
        if result.messages:
            # Join multiple messages into one text field for now
            message = "; ".join([m for m in result.messages if m])

        # --- inside log_creation_event() ---

        payload = {
            "canonical_handle": request.canonical_handle,
            "canonical_title": request.canonical_title,
            "damaged_handle": result.damaged_handle,   # <-- corrected
            "damaged_title": None,                     # <-- derived only inside product_service; not stored now
            "damaged_product_id": str(result.damaged_product_id) if result.damaged_product_id else None,
            "variants_json": variants_payload,
            "operator": operator,
            "dry_run": request.dry_run,
            "status": result.status,
            "message": message,
        }

        supabase.schema("damaged").from_("creation_log").insert(payload).execute()
        logger.info(
            "[CreationLog] wrote creation_log row canonical=%s damaged=%s status=%s",
            request.canonical_handle,
            payload["damaged_handle"],
            result.status,
        )

    except Exception as e:
        # Never break the main flow because of logging issues.
        logger.warning("[CreationLog] failed to write creation_log row: %s", e)