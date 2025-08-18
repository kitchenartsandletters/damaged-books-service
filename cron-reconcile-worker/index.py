import os
import sys
from pprint import pprint
from dotenv import load_dotenv

# Add the root of the repo to Python path so we can import `tasks.cron_tasks`
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tasks.cron_tasks import run_reconcile  # ✅ this resolves to your correct file

if __name__ == "__main__":
    try:
        result = run_reconcile()
        print("✅ Reconcile completed")
        pprint(result)
    except Exception as e:
        print(f"❌ Reconcile run failed: {e}")