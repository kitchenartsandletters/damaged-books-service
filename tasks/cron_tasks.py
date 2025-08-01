# tasks/cron_tasks.py

from celery import shared_task
from services.cron_service import get_all_hurt_books
from services.used_book_manager import process_inventory_change
from services.backup_service import backup_redirects
from services.notification_service import notify_critical_error
import logging

logger = logging.getLogger(__name__)


@shared_task
def process_all_hurt_books():
    """
    Scheduled inventory processing task.
    """
    try:
        logger.info("🔁 Running scheduled hurt book inventory scan...")
        hurt_books = asyncio.run(get_all_hurt_books())

        for product in hurt_books:
            for variant in product.get("variants", []):
                asyncio.run(process_inventory_change(
                    variant["inventory_item_id"],
                    variant["id"],
                    product["id"]
                ))

        logger.info("✅ Completed inventory scan task.")

    except Exception as e:
        logger.error(f"❌ Error in inventory scan task: {str(e)}")
        notify_critical_error(str(e), context="Scheduled inventory check")


@shared_task
def daily_backup():
    """
    Daily backup task.
    """
    try:
        logger.info("🧩 Starting daily backup...")
        result = backup_redirects()
        if result:
            logger.info(f"✅ Backup complete: {result['count']} redirects saved.")
        else:
            logger.warning("⚠️ Backup returned no results.")
    except Exception as e:
        logger.error(f"❌ Error in backup task: {str(e)}")
        notify_critical_error(str(e), context="Daily backup")