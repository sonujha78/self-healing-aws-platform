# Self-Healing AWS Infrastructure Automation Platform

Python-based automation platform that provisions AWS infra with pure boto3 (no Terraform),
configures it with Ansible (including a custom Ansible module), and continuously
monitors + self-heals unhealthy EC2 instances.

## Status
🚧 In progress — building part by part.

## Stack
Python 3, boto3, Click, Ansible, Flask/FastAPI, Paramiko, CloudWatch, systemd

## Parts
- [ ] Part A — Infra Provisioning (boto3 CLI)
- [ ] Part B — Ansible Config Management + Custom Module
- [ ] Part C — CloudWatch Monitoring
- [ ] Part D — Self-Healing Daemon
- [ ] Failure Test Evidence
- [ ] Docs
