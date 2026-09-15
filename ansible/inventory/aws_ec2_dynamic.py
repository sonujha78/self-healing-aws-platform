#!/usr/bin/env python3
"""
Custom Ansible dynamic inventory script.

Replaces a static hosts file entirely: queries AWS live via boto3 for all
running EC2 instances tagged with this project, and emits Ansible's
expected dynamic-inventory JSON structure (--list / --host contract).

Usage (called by Ansible automatically, or manually for debugging):
    ./aws_ec2_dynamic.py --list
    ./aws_ec2_dynamic.py --host <hostname>
"""

import json
import sys
import boto3

PROJECT_TAG_KEY = "Project"
PROJECT_TAG_VALUE = "self-healing-platform"
GROUP_NAME = "self_healing_nodes"


def get_instances():
    client = boto3.client("ec2")
    resp = client.describe_instances(
        Filters=[
            {"Name": f"tag:{PROJECT_TAG_KEY}", "Values": [PROJECT_TAG_VALUE]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    instances = []
    for reservation in resp.get("Reservations", []):
        instances.extend(reservation.get("Instances", []))
    return instances


def instance_name(instance):
    for tag in instance.get("Tags", []):
        if tag["Key"] == "Name":
            return tag["Value"]
    return instance["InstanceId"]


def build_inventory():
    instances = get_instances()

    hosts = []
    hostvars = {}

    for inst in instances:
        name = instance_name(inst)
        public_ip = inst.get("PublicIpAddress")
        if not public_ip:
            # skip instances without a public IP - nothing to SSH into
            continue

        hosts.append(name)
        hostvars[name] = {
            "ansible_host": public_ip,
            "instance_id": inst["InstanceId"],
            "private_ip": inst.get("PrivateIpAddress"),
            "instance_state": inst["State"]["Name"],
            "instance_type": inst["InstanceType"],
            "availability_zone": inst["Placement"]["AvailabilityZone"],
            "ansible_python_interpreter": "/usr/bin/python3",
        }

    inventory = {
        GROUP_NAME: {
            "hosts": hosts,
            "vars": {},
        },
        "_meta": {
            "hostvars": hostvars,
        },
    }
    return inventory


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--list":
        print(json.dumps(build_inventory(), indent=2))
    elif len(sys.argv) >= 3 and sys.argv[1] == "--host":
        # _meta.hostvars in --list already covers this; return empty dict
        # per the dynamic inventory contract.
        print(json.dumps({}))
    else:
        sys.stderr.write("Usage: aws_ec2_dynamic.py --list | --host <hostname>\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
