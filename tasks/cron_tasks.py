import asyncio
from services.cron_service import reconcile_damaged_inventory
from services import cron_service

def run_reconcile():
    inspected, updated, skipped = cron_service.reconcile()
    print(f"[reconcile] inspected={inspected} updated={updated} skipped={skipped}")
    return inspected, updated, skipped

if __name__ == "__main__":
    asyncio.run(reconcile_damaged_inventory())