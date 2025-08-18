import asyncio
from services.cron_service import reconcile_damaged_inventory

def run_reconcile():
    result = asyncio.run(reconcile_damaged_inventory())
    return result  # Just return the full dictionary