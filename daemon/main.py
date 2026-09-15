#!/usr/bin/env python3
"""
Entrypoint for the self-healing daemon.
Run as a systemd service. Starts:
  - the Flask /status API in a background thread
  - the main health-check / self-heal loop in the foreground
"""

import threading
import logging

from daemon.logging_config import setup_logging
from daemon.cw_logs import CloudWatchLogPusher
from daemon.healer import SelfHealingDaemon
from daemon import api


def main():
    setup_logging()
    logger = logging.getLogger("self_healing_daemon")

    try:
        cw_pusher = CloudWatchLogPusher()
    except Exception as e:
        logger.warning("Could not initialize CloudWatch Logs pusher, continuing with local logs only: %s", e)
        cw_pusher = None

    daemon = SelfHealingDaemon(cw_pusher=cw_pusher)
    api.attach_daemon(daemon)

    flask_thread = threading.Thread(
        target=lambda: api.app.run(host="0.0.0.0", port=5000, use_reloader=False),
        daemon=True,
    )
    flask_thread.start()

    daemon.run_forever()


if __name__ == "__main__":
    main()
