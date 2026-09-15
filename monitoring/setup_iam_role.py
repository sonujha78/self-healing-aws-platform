#!/usr/bin/env python3
"""
One-time setup: creates an IAM role + instance profile that grants EC2
instances permission to push CloudWatch custom metrics and write log
events, without embedding long-lived credentials on the instance.
Attaches the profile to all currently-running project instances.

Idempotent - safe to re-run.
"""

import json
import time
import logging

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ROLE_NAME = "self-healing-platform-role"
PROFILE_NAME = "self-healing-platform-profile"
POLICY_NAME = "self-healing-platform-cloudwatch-policy"
PROJECT_TAG_VALUE = "self-healing-platform"

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ec2.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}

PERMISSIONS_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": [
            "cloudwatch:PutMetricData",
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
            "logs:DescribeLogStreams",
        ],
        "Resource": "*",
    }],
}


def ensure_role(iam):
    try:
        iam.get_role(RoleName=ROLE_NAME)
        logger.info("IAM role already exists: %s", ROLE_NAME)
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST_POLICY),
            Description="Allows self-healing-platform EC2 instances to push CloudWatch metrics/logs",
        )
        logger.info("Created IAM role: %s", ROLE_NAME)

    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName=POLICY_NAME,
        PolicyDocument=json.dumps(PERMISSIONS_POLICY),
    )
    logger.info("Attached inline policy to role.")


def ensure_instance_profile(iam):
    try:
        iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)
        logger.info("Instance profile already exists: %s", PROFILE_NAME)
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        iam.create_instance_profile(InstanceProfileName=PROFILE_NAME)
        logger.info("Created instance profile: %s", PROFILE_NAME)
        time.sleep(5)  # IAM eventual consistency

    profile = iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)
    existing_roles = [r["RoleName"] for r in profile["InstanceProfile"]["Roles"]]
    if ROLE_NAME not in existing_roles:
        iam.add_role_to_instance_profile(InstanceProfileName=PROFILE_NAME, RoleName=ROLE_NAME)
        logger.info("Added role %s to instance profile %s", ROLE_NAME, PROFILE_NAME)
        time.sleep(5)


def attach_to_instances(ec2):
    resp = ec2.describe_instances(Filters=[
        {"Name": "tag:Project", "Values": [PROJECT_TAG_VALUE]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    instance_ids = [
        i["InstanceId"]
        for r in resp["Reservations"]
        for i in r["Instances"]
    ]

    for iid in instance_ids:
        existing = ec2.describe_iam_instance_profile_associations(
            Filters=[{"Name": "instance-id", "Values": [iid]}]
        )["IamInstanceProfileAssociations"]

        if existing:
            logger.info("Instance %s already has an instance profile attached.", iid)
            continue

        try:
            ec2.associate_iam_instance_profile(
                IamInstanceProfile={"Name": PROFILE_NAME},
                InstanceId=iid,
            )
            logger.info("Attached instance profile to %s", iid)
        except ClientError as e:
            logger.warning("Could not attach profile to %s: %s", iid, e)


def main():
    iam = boto3.client("iam")
    ec2 = boto3.client("ec2")

    ensure_role(iam)
    ensure_instance_profile(iam)
    attach_to_instances(ec2)

    logger.info("IAM setup complete.")


if __name__ == "__main__":
    main()
