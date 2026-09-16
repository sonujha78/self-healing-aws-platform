"""
Core self-healing logic.

For each running project instance:
  - Checks EC2 status checks (CloudWatch/EC2) + the custom app_health_score
    metric (Part C).
  - Tracks consecutive failures per instance in memory.
  - Skips instances that are still within their warm-up grace period
    (AWS EC2 status checks typically take 2-3 minutes to first report
    "ok" after launch - checking too early causes false-positive failures).
  - If an instance fails N consecutive checks AFTER its grace period:
      1. Terminate it via boto3.
      2. Provision a replacement by importing Part A's AWSManager directly
         (not a copy-pasted script).
      3. WAIT until the replacement has a public IP and is reachable on
         port 22 (SSH) - AWS has a short propagation lag before a newly
         "running" instance's public IP is assigned/reachable, and running
         Ansible before that silently configures zero hosts.
      4. Re-run Ansible configuration against the new instance via subprocess.
      5. Log every action with timestamp + reason, locally AND to CloudWatch Logs.
"""

import socket
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
WARMUP_GRACE_SECONDS = 300      # skip health checks for this long after launch
SSH_READY_TIMEOUT_SECONDS = 180 # max time to wait for new instance to be SSH-reachable

ANSIBLE_DIR = "/home/sonu/self-healing-aws-platform/ansible"
ANSIBLE_PLAYBOOK = "playbooks/site.yml"
SSH_KEY_NAME = "self-healing-key"
VENV_BIN = "/home/sonu/self-healing-aws-platform/.venv/bin"
ANSIBLE_PLAYBOOK_BIN = "ansible-playbook"  # resolved via PATH (system-installed), but PATH is
                                            # rewritten below so the child inventory script's
                                            # "#!/usr/bin/env python3" resolves to the venv's
                                            # python3, which has boto3 installed.


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
                instances.append({
                    "id": i["InstanceId"],
                    "name": name,
                    "launch_time": i["LaunchTime"],
                })
        return instances

    def is_within_warmup(self, launch_time) -> bool:
        age = datetime.now(timezone.utc) - launch_time
        return age < timedelta(seconds=WARMUP_GRACE_SECONDS)

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

    def provision_replacement(self) -> list:
        manager = AWSManager(region=self.region)
        result = manager.provision_all(key_name=SSH_KEY_NAME)
        new_ids = result["instance_ids"]
        self._log_event(
            "info", f"Replacement provisioning result: {new_ids}",
            action="provision_complete",
        )
        return new_ids

    def wait_for_instances_sshable(self, instance_ids):
        """
        Waits until every instance has a public IP AND port 22 is accepting
        connections. Prevents running Ansible before AWS has finished
        propagating the public IP / before sshd is up - which otherwise
        causes the dynamic inventory to silently skip the host.
        """
        self._log_event("info", "Waiting for replacement instance(s) to become SSH-reachable", action="wait_ssh_start")
        deadline = time.monotonic() + SSH_READY_TIMEOUT_SECONDS

        pending = set(instance_ids)
        while pending and time.monotonic() < deadline:
            resp = self.ec2.describe_instances(InstanceIds=list(pending))
            for r in resp["Reservations"]:
                for inst in r["Instances"]:
                    iid = inst["InstanceId"]
                    ip = inst.get("PublicIpAddress")
                    if not ip:
                        continue
                    try:
                        with socket.create_connection((ip, 22), timeout=3):
                            pending.discard(iid)
                            self._log_event(
                                "info", f"Instance is SSH-reachable at {ip}",
                                instance_id=iid, action="ssh_ready",
                            )
                    except OSError:
                        pass
            if pending:
                time.sleep(5)

        if pending:
            self._log_event(
                "warning", f"Timed out waiting for SSH readiness on: {pending}",
                action="wait_ssh_timeout",
            )
        else:
            self._log_event("info", "All replacement instance(s) SSH-reachable", action="wait_ssh_complete")

    def reconfigure_with_ansible(self):
        self._log_event("info", "Re-running Ansible configuration", action="ansible_start")
        try:
            import os
            env = os.environ.copy()
            env["PATH"] = f"{VENV_BIN}:{env.get('PATH', '')}"

            result = subprocess.run(
                [ANSIBLE_PLAYBOOK_BIN, ANSIBLE_PLAYBOOK],
                cwd=ANSIBLE_DIR,
                capture_output=True,
                text=True,
                timeout=600,
                env=env,
            )
            if result.returncode == 0:
                self._log_event(
                    "info",
                    f"Ansible reconfiguration succeeded: {result.stdout[-300:]}",
                    action="ansible_complete",
                )
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
        new_ids = self.provision_replacement()
        self.wait_for_instances_sshable(new_ids)
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

            if self.is_within_warmup(inst["launch_time"]):
                cycle_status[iid] = {
                    "name": inst["name"],
                    "healthy": None,
                    "reason": "within warm-up grace period, skipping check",
                    "consecutive_failures": 0,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                self._log_event(
                    "info", "Skipping health check - instance still warming up",
                    instance_id=iid,
                )
                continue

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
