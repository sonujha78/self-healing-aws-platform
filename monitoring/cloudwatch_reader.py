#!/usr/bin/env python3
"""
Pulls CloudWatch metrics (CPU utilization, EC2 status checks) for all
tagged project instances, on a schedule (run manually or via cron).

Free-tier note: this only READS existing AWS/EC2 namespace metrics
(which are free), it does not create any custom metrics itself -
that's push_custom_metric.py's job, kept to 1-2 custom metrics total
per the project's free-tier policy.
"""

import logging
from datetime import datetime, timedelta, timezone

import boto3
import click

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_TAG_VALUE = "self-healing-platform"


def get_tagged_instances(ec2):
    resp = ec2.describe_instances(Filters=[
        {"Name": "tag:Project", "Values": [PROJECT_TAG_VALUE]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    instances = []
    for r in resp["Reservations"]:
        for i in r["Instances"]:
            name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), i["InstanceId"])
            instances.append({"id": i["InstanceId"], "name": name})
    return instances


def get_cpu_utilization(cw, instance_id, minutes=10):
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    resp = cw.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start,
        EndTime=end,
        Period=300,
        Statistics=["Average"],
    )
    datapoints = sorted(resp["Datapoints"], key=lambda d: d["Timestamp"])
    return datapoints[-1]["Average"] if datapoints else None


def get_status_check(ec2, instance_id):
    resp = ec2.describe_instance_status(InstanceIds=[instance_id])
    statuses = resp.get("InstanceStatuses", [])
    if not statuses:
        return None, None
    s = statuses[0]
    return s["InstanceStatus"]["Status"], s["SystemStatus"]["Status"]


@click.command()
@click.option("--region", default=None, help="AWS region (defaults to configured region).")
def main(region):
    ec2 = boto3.client("ec2", region_name=region)
    cw = boto3.client("cloudwatch", region_name=region)

    instances = get_tagged_instances(ec2)
    if not instances:
        logger.info("No running tagged instances found.")
        return

    logger.info("--- CloudWatch metrics for %d instance(s) ---", len(instances))
    for inst in instances:
        cpu = get_cpu_utilization(cw, inst["id"])
        instance_status, system_status = get_status_check(ec2, inst["id"])
        logger.info(
            "%-32s %-20s CPU=%-8s InstanceStatus=%-8s SystemStatus=%s",
            inst["name"],
            inst["id"],
            f"{cpu:.2f}%" if cpu is not None else "N/A",
            instance_status or "N/A",
            system_status or "N/A",
        )


if __name__ == "__main__":
    main()
