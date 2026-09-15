"""
Small Flask app exposing /status - shows current fleet health without
needing to read raw logs. Runs in a background thread alongside the
main healing loop.
"""

from flask import Flask, jsonify
from datetime import datetime, timezone

app = Flask(__name__)
_daemon_ref = None  # set by main.py at startup


def attach_daemon(daemon_instance):
    global _daemon_ref
    _daemon_ref = daemon_instance


@app.route("/status")
def status():
    if _daemon_ref is None:
        return jsonify({"error": "daemon not initialized"}), 503

    return jsonify({
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "failure_threshold": _daemon_ref.__class__.__module__ and 3,
        "fleet": _daemon_ref.fleet_status,
    })


@app.route("/")
def index():
    return jsonify({"service": "self-healing-platform daemon", "endpoints": ["/status"]})
