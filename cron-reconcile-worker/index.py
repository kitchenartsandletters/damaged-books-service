# index.py

import os
import sys
from dotenv import load_dotenv

# Ensure parent directory (DBS project root) is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.tasks.cron_tasks import run_reconcile

load_dotenv()

if __name__ == "__main__":
    try:
        inspected, updated, skipped = run_reconcile()
        print(f"✅ Reconcile run complete: inspected={inspected}, updated={updated}, skipped={skipped}")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Reconcile run failed: {e}")
        sys.exit(1)