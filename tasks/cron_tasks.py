import asyncio
from services.cron_service import reconcile_damaged_inventory

def run_reconcile():
    return asyncio.run(reconcile_damaged_inventory())