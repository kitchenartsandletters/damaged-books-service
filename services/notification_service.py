# services/notification_service.py

import logging

logger = logging.getLogger(__name__)

def notify(level: str, title: str, message: str):
    """
    Send a notification to Slack, logging service, or dashboard.
    `level` can be "info", "warning", "error"
    """
    formatted_message = f"[{level.upper()}] {title}: {message}"
    if level == "warning":
        logger.warning(formatted_message)
    elif level == "error":
        logger.error(formatted_message)
    else:
        logger.info(formatted_message)

def notify_critical_error(exception: Exception, context: dict = None):
    """
    Notify critical errors with traceback and context.
    """
    logger.error("🔴 Critical Error: %s", str(exception))
    if context:
        logger.error("Context: %s", context)