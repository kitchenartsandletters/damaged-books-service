import logging
import os
import sys
from pprint import pprint

# Configure logging BEFORE importing services, so INFO/ERROR lines from the
# reconcile actually reach Railway logs (Python's default drops < WARNING).
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

# Add the root of the repo to Python path so we can import `tasks.cron_tasks`
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tasks.cron_tasks import run_reconcile  # noqa: E402

# Fraction of inspected rows allowed to fail before the run is a failure.
MAX_FAILURE_RATIO = float(os.getenv("RECONCILE_MAX_FAILURE_RATIO", "0.10"))


def evaluate(result: dict) -> tuple[bool, str]:
    """Return (ok, reason). Any run that did not do its job is not ok."""
    inspected = int(result.get("inspected") or 0)
    updated = int(result.get("updated") or 0)
    failed = int(result.get("failed", result.get("skipped")) or 0)
    note = result.get("note")

    if note and "SHOPIFY_LOCATION_ID" in note:
        return False, note
    if inspected > 0 and updated == 0:
        return False, f"0/{inspected} rows written ({failed} failed)"
    if inspected > 0 and failed / inspected > MAX_FAILURE_RATIO:
        return False, f"{failed}/{inspected} rows failed (> {MAX_FAILURE_RATIO:.0%})"
    touched = int(result.get("products_touched") or 0)
    rule_failures = int(result.get("rule_failures") or 0)
    if touched > 0 and rule_failures / touched > MAX_FAILURE_RATIO:
        return False, f"product rules failed for {rule_failures}/{touched} products"
    return True, f"{updated}/{inspected} rows written, {failed} failed"


def main() -> int:
    try:
        result = run_reconcile()
    except Exception as e:
        logging.exception(f"❌ Reconcile run crashed: {e}")
        return 1

    pprint(result)
    ok, reason = evaluate(result)
    if ok:
        print(f"✅ Reconcile completed: {reason}")
        return 0
    print(f"❌ Reconcile FAILED: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
