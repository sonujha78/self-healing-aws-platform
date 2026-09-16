# Mandatory Failure Test — Evidence

## Test setup
- Fleet: 2× t3.micro EC2 instances, self-healing daemon running as a systemd
  service, health checks every 30s, failure threshold = 3 consecutive fails,
  new-instance warm-up grace period = 300s.
- Failure injected by SSH'ing into a running instance and manually stopping
  the health app's systemd service (`sudo systemctl stop healthapp`) —
  simulating an unexpected application crash, per the task's test method.

## Timeline (IST, 2026-09-16)

| Event | Timestamp |
|---|---|
| Failure injected (health app stopped) on `i-0c76ee943e082fe3a` | 09:47:56 |
| Health check failed 1/3 | 09:51:28 |
| Health check failed 2/3 | 09:52:06 |
| Health check failed 3/3 — threshold reached | 09:52:39 |
| **Detection: self-heal sequence started** | 09:52:41 |
| Unhealthy instance termination started | 09:52:41 |
| **Instance terminated** | 09:53:23 |
| **Replacement instance provisioned** (`i-068b128e8dc2d5545`) | 09:53:54 |
| Replacement confirmed SSH-reachable | 09:53:55 |
| Ansible reconfiguration started | 09:53:55 |
| **Ansible reconfiguration succeeded** (both hosts, 0 failures) | 09:56:26 |
| **Self-heal sequence complete** | 09:56:32 |
| Manual verification: `curl http://<new-ip>/health` → `{"status":"ok",...}` | 09:58:44 |

**Total time from failure injection to fully verified healthy replacement: ~11 minutes**
(includes ~4.5 min of detection latency by design — 3 consecutive 30s-interval
checks — plus ~2.5 min for a genuine full Ansible configuration run: user/SSH
hardening, nginx, Flask app, logrotate, and CloudWatch metric push setup.)

## What was proven, hands-off, with no manual intervention after failure injection
1. **Detection** — the daemon detected the failing health endpoint via the
   custom `app_health_score` CloudWatch metric.
2. **Termination** — the unhealthy instance was terminated via boto3.
3. **Replacement provisioning** — a new instance was provisioned by
   reusing Part A's `AWSManager` as an imported library (not a copy-pasted
   script), with a uniquely generated Name tag.
4. **Reconfiguration** — Ansible was re-run automatically via subprocess
   against the new instance (using the dynamic inventory), successfully
   reinstalling nginx, the Flask health app, SSH hardening, logrotate, and
   the CloudWatch metric push script — verified by a 0-failure PLAY RECAP
   and a working `/health` endpoint.

## Bugs found and fixed during testing (for the interview write-up)
- **False-positive healing on freshly launched instances**: AWS EC2 status
  checks take 2–3 minutes to first report "ok" after launch. Fixed by adding
  a warm-up grace period before a new instance enters the health-check loop.
- **Ansible dynamic inventory hostvars collision**: instances were originally
  named by position (`node-1`, `node-2`), so a freshly launched replacement
  could collide with a name still in AWS tags, silently dropping one host
  from Ansible's inventory. Fixed by deriving each instance's Name tag from
  its own unique instance ID.
- **Subprocess PATH mismatch**: the daemon (running under systemd) invoked
  `ansible-playbook` without the project's virtualenv on `PATH`, so the
  dynamic inventory script's `boto3` import failed silently, causing
  "no hosts matched" and a false "success". Fixed by explicitly prepending
  the venv's `bin/` directory to the subprocess's `PATH`.
- **Metric propagation delay**: a freshly provisioned instance's first
  `app_health_score` CloudWatch datapoint could arrive after the health
  daemon's first check. Fixed by having the metric-push Ansible playbook
  run the push script once immediately at deploy time, in addition to
  scheduling it via cron.
