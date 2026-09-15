#!/usr/bin/env python3
import logging
import socket

import boto3
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

NAMESPACE = "SelfHealingPlatform"
METRIC_NAME = "app_health_score"
HEALTH_URL = "http://127.0.0.1/health"
TIMEOUT_SECONDS = 5
METADATA_BASE = "http://169.254.169.254/latest"


def _imds_token():
    resp = requests.put(
        f"{METADATA_BASE}/api/token",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        timeout=2,
    )
    return resp.text


def get_instance_id(token):
    try:
        resp = requests.get(
            f"{METADATA_BASE}/meta-data/instance-id",
            headers={"X-aws-ec2-metadata-token": token},
            timeout=2,
        )
        return resp.text
    except requests.RequestException:
        return socket.gethostname()


def get_region(token):
    try:
        resp = requests.get(
            f"{METADATA_BASE}/meta-data/placement/region",
            headers={"X-aws-ec2-metadata-token": token},
            timeout=2,
        )
        return resp.text
    except requests.RequestException:
        return None


def check_health():
    try:
        resp = requests.get(HEALTH_URL, timeout=TIMEOUT_SECONDS)
        if resp.status_code == 200 and resp.json().get("status") == "ok":
            return 1.0
        return 0.0
    except requests.RequestException as e:
        logger.warning("Health check failed: %s", e)
        return 0.0


def push_metric(cw, instance_id, score):
    cw.put_metric_data(
        Namespace=NAMESPACE,
        MetricData=[{
            "MetricName": METRIC_NAME,
            "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
            "Value": score,
            "Unit": "None",
        }],
    )
    logger.info("Pushed %s=%.1f for instance %s", METRIC_NAME, score, instance_id)


def main():
    token = _imds_token()
    instance_id = get_instance_id(token)
    region = get_region(token)

    score = check_health()
    cw = boto3.client("cloudwatch", region_name=region)
    push_metric(cw, instance_id, score)


if __name__ == "__main__":
    main()
