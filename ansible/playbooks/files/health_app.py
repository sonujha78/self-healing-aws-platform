#!/usr/bin/env python3
"""Minimal sample app exposing a health endpoint for monitoring/self-healing to check."""
import time
import socket
from flask import Flask, jsonify

app = Flask(__name__)
START_TIME = time.time()

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "hostname": socket.gethostname(),
        "uptime_seconds": round(time.time() - START_TIME, 2),
    }), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
