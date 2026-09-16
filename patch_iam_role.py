import boto3
import json
import time

REGION = "ap-south-1"
INSTANCE_IDS = ["i-0cb9c6cb45b523036", "i-083597d0902550a7a"]

ROLE_NAME = "self-healing-metric-push-role"
PROFILE_NAME = "self-healing-metric-push-profile"

iam = boto3.client("iam", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)

trust_policy = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ec2.amazonaws.com"},
        "Action": "sts:AssumeRole"
    }]
}

try:
    iam.get_role(RoleName=ROLE_NAME)
    print("Role already exists")
except iam.exceptions.NoSuchEntityException:
    iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust_policy))
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="cloudwatch-put-metric-only",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*"}]
        })
    )
    print("Role created")

try:
    iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)
    print("Profile already exists")
except iam.exceptions.NoSuchEntityException:
    iam.create_instance_profile(InstanceProfileName=PROFILE_NAME)
    iam.add_role_to_instance_profile(InstanceProfileName=PROFILE_NAME, RoleName=ROLE_NAME)
    print("Profile created, waiting for propagation...")
    time.sleep(10)

for iid in INSTANCE_IDS:
    try:
        ec2.associate_iam_instance_profile(IamInstanceProfile={"Name": PROFILE_NAME}, InstanceId=iid)
        print(f"Attached profile to {iid}")
    except Exception as e:
        print(f"Failed on {iid}: {e}")
