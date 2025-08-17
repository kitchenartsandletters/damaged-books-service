import asyncio
from services.cron_service import reconcile_damaged_inventory

if __name__ == "__main__":
    asyncio.run(reconcile_damaged_inventory())