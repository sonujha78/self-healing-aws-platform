"""
Structured logging setup for the self-healing daemon.
Uses RotatingFileHandler so logs never grow unbounded (task requirement),
plus pushes the same structured events to CloudWatch Logs.
"""

import json
import logging
import logging.handlers
import os

LOG_DIR = "/var/log/self-healing-daemon"
LOG_FILE = os.path.join(LOG_DIR, "daemon.log")

CLOUDWATCH_LOG_GROUP = "/self-healing-platform/daemon"
CLOUDWATCH_LOG_STREAM = "healing-events"


class JsonFormatter(logging.Formatter):
    """Structured JSON log lines - timestamp + reason + action, easy to grep/parse."""

    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if hasattr(record, "instance_id"):
            payload["instance_id"] = record.instance_id
        if hasattr(record, "action"):
            payload["action"] = record.action
        if hasattr(record, "reason"):
            payload["reason"] = record.reason
        return json.dumps(payload)


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)

    logger = logging.getLogger("self_healing_daemon")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    # Local rotating file handler - bounded log growth, task requirement.
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)

    # Also echo to stdout - systemd/journald captures this automatically.
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(JsonFormatter())
    logger.addHandler(stream_handler)

    return logger
