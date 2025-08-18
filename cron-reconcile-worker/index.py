from ..tasks.cron_tasks import run_reconcile
from pprint import pprint

if __name__ == "__main__":
    try:
        result = run_reconcile()
        print("✅ Reconcile completed")
        pprint(result)
    except Exception as e:
        print(f"❌ Reconcile run failed: {e}")