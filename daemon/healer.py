"""
Core self-healing logic.

For each running project instance:
  - Checks EC2 status checks (CloudWatch/EC2) + the custom app_health_score
    metric (Part C).
  - Tracks consecutive failures per instance in memory.
  - If an instance fails N consecutive checks:
      1. Terminate it via boto3.
      2. Provision a replacement by importing Part A's AWSManager directly
         (not a copy-pasted script).
      3. Re-run Ansible configuration against the new instance via subprocess.
      4. Log every action with timestamp + reason, locally AND to CloudWatch Logs.
"""

import subprocess
import logging
import time
from datetime import datetime, timedelta, timezone

import boto3

from provisioning.aws_manager import AWSManager
from provisioning import config as prov_config

logger = logging.getLogger("self_healing_daemon")

PROJECT_TAG_VALUE = "self-healing-platform"
NAMESPACE = "SelfHealingPlatform"
METRIC_NAME = "app_health_score"

FAILURE_THRESHOLD = 3          # N consecutive failed checks before healing
CHECK_INTERVAL_SECONDS = 30
METRIC_LOOKBACK_MINUTES = 5

ANSIBLE_DIR = "/home/sonu/self-healing-aws-platform/ansible"
ANSIBLE_PLAYBOOK = "playbooks/site.yml"
SSH_KEY_NAME = "self-healing-key"


class InstanceHealth:
    """In-memory consecutive-failure tracker, keyed by instance id."""
    def __init__(self):
        self.consecutive_failures = {}

    def record(self, instance_id: str, healthy: bool):
        if healthy:
            self.consecutive_failures[instance_id] = 0
        else:
            self.consecutive_failures[instance_id] = self.consecutive_failures.get(instance_id, 0) + 1
        return self.consecutive_failures[instance_id]

    def forget(self, instance_id: str):
        self.consecutive_failures.pop(instance_id, None)


class SelfHealingDaemon:
    def __init__(self, region: str = None, cw_pusher=None):
        self.region = region or prov_config.get_region()
        self.ec2 = boto3.client("ec2", region_name=self.region)
        self.cw = boto3.client("cloudwatch", region_name=self.region)
        self.health = InstanceHealth()
        self.cw_pusher = cw_pusher
        self.fleet_status = {}  # exposed to the Flask /status endpoint

    # ---------------- logging helper (local + CloudWatch) ----------------

    def _log_event(self, level, message, instance_id=None, action=None, reason=None):
        extra = {}
        if instance_id:
            extra["instance_id"] = instance_id
        if action:
            extra["action"] = action
        if reason:
            extra["reason"] = reason

        getattr(logger, level)(message, extra=extra)

        if self.cw_pusher:
            ts = datetime.now(timezone.utc).isoformat()
            parts = [ts, message]
            if instance_id:
                parts.append(f"instance={instance_id}")
            if action:
                parts.append(f"action={action}")
            if reason:
                parts.append(f"reason={reason}")
            self.cw_pusher.push(" | ".join(parts))

    # ---------------- health evaluation ----------------

    def get_tagged_instances(self):
        resp = self.ec2.describe_instances(Filters=[
            {"Name": "tag:Project", "Values": [PROJECT_TAG_VALUE]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ])
        instances = []
        for r in resp["Reservations"]:
            for i in r["Instances"]:
                name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), i["InstanceId"])
                instances.append({"id": i["InstanceId"], "name": name})
        return instances

    def get_ec2_status_ok(self, instance_id) -> bool:
        resp = self.ec2.describe_instance_status(InstanceIds=[instance_id])
        statuses = resp.get("InstanceStatuses", [])
        if not statuses:
            return False
        s = statuses[0]
        return (
            s["InstanceStatus"]["Status"] == "ok"
            and s["SystemStatus"]["Status"] == "ok"
        )

    def get_app_health_score(self, instance_id) -> float:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=METRIC_LOOKBACK_MINUTES)
        resp = self.cw.get_metric_statistics(
            Namespace=NAMESPACE,
            MetricName=METRIC_NAME,
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=start,
            EndTime=end,
            Period=60,
            Statistics=["Average"],
        )
        datapoints = sorted(resp["Datapoints"], key=lambda d: d["Timestamp"])
        if not datapoints:
            # No recent datapoint = treat as unknown/unhealthy (app may be down
            # or not reporting - either way, don't assume healthy).
            return 0.0
        return datapoints[-1]["Average"]

    def check_instance_health(self, instance_id) -> tuple[bool, str]:
        ec2_ok = self.get_ec2_status_ok(instance_id)
        app_score = self.get_app_health_score(instance_id)
        healthy = ec2_ok and app_score >= 1.0

        if healthy:
            reason = "ok"
        elif not ec2_ok:
            reason = "EC2 status check failed"
        else:
            reason = f"app_health_score={app_score} (below threshold)"

        return healthy, reason

    # ---------------- healing actions ----------------

    def terminate_instance(self, instance_id):
        self._log_event(
            "warning", "Terminating unhealthy instance",
            instance_id=instance_id, action="terminate",
        )
        self.ec2.terminate_instances(InstanceIds=[instance_id])
        self.ec2.get_waiter("instance_terminated").wait(InstanceIds=[instance_id])
        self._log_event(
            "info", "Instance terminated", instance_id=instance_id, action="terminate_complete",
        )

    def provision_replacement(self) -> str:
        """Reuses Part A's AWSManager as an imported library, not a copy-pasted script."""
        manager = AWSManager(region=self.region)
        result = manager.provision_all(key_name=SSH_KEY_NAME)
        new_ids = result["instance_ids"]
        self._log_event(
            "info", f"Replacement provisioning result: {new_ids}",
            action="provision_complete",
        )
        return new_ids

    def reconfigure_with_ansible(self):
        self._log_event("info", "Re-running Ansible configuration", action="ansible_start")
        try:
            result = subprocess.run(
                ["ansible-playbook", ANSIBLE_PLAYBOOK],
                cwd=ANSIBLE_DIR,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode == 0:
                self._log_event("info", "Ansible reconfiguration succeeded", action="ansible_complete")
            else:
                self._log_event(
                    "error",
                    f"Ansible reconfiguration failed (rc={result.returncode}): {result.stderr[-500:]}",
                    action="ansible_failed",
                )
        except subprocess.TimeoutExpired:
            self._log_event("error", "Ansible reconfiguration timed out", action="ansible_timeout")

    def heal(self, instance_id):
        self._log_event(
            "warning", "Instance exceeded failure threshold, starting self-heal sequence",
            instance_id=instance_id, action="heal_start",
        )
        self.terminate_instance(instance_id)
        self.health.forget(instance_id)
        self.provision_replacement()
        self.reconfigure_with_ansible()
        self._log_event(
            "info", "Self-heal sequence complete", instance_id=instance_id, action="heal_complete",
        )

    # ---------------- main loop ----------------

    def run_check_cycle(self):
        instances = self.get_tagged_instances()
        cycle_status = {}

        for inst in instances:
            iid = inst["id"]
            healthy, reason = self.check_instance_health(iid)
            failures = self.health.record(iid, healthy)

            cycle_status[iid] = {
                "name": inst["name"],
                "healthy": healthy,
                "reason": reason,
                "consecutive_failures": failures,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

            if healthy:
                self._log_event("info", "Health check passed", instance_id=iid, reason=reason)
            else:
                self._log_event(
                    "warning", f"Health check failed ({failures}/{FAILURE_THRESHOLD})",
                    instance_id=iid, reason=reason,
                )

            if failures >= FAILURE_THRESHOLD:
                self.heal(iid)

        self.fleet_status = cycle_status
        return cycle_status

    def run_forever(self):
        self._log_event("info", "Self-healing daemon started", action="daemon_start")
        while True:
            try:
                self.run_check_cycle()
            except Exception as e:
                self._log_event("error", f"Unexpected error in check cycle: {e}", action="cycle_error")
            time.sleep(CHECK_INTERVAL_SECONDS)
