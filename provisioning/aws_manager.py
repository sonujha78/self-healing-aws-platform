"""
Core boto3 automation — VPC, Subnet, Security Group, EC2 instances.
Every create_* function is idempotent: it checks by tag/Name before creating.
This module is imported directly by the Part D self-healing daemon —
it is NOT just a CLI script.
"""

import time
import logging

import boto3
from botocore.exceptions import ClientError

from provisioning import config

logger = logging.getLogger(__name__)


def _tag_spec(resource_type: str, name: str) -> list:
    tags = {**config.TAGS, "Name": name}
    return [{
        "ResourceType": resource_type,
        "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
    }]


class AWSManager:
    def __init__(self, region: str = None, dry_run: bool = False):
        self.region = region or config.get_region()
        self.dry_run = dry_run
        self.ec2 = boto3.resource("ec2", region_name=self.region)
        self.client = boto3.client("ec2", region_name=self.region)
        self.iam = boto3.client("iam", region_name=self.region)
        self.instance_type = config.get_instance_type_for_region(self.region)
        config.validate_instance_type(self.instance_type)

    METRIC_ROLE_NAME = "self-healing-metric-push-role"
    METRIC_PROFILE_NAME = "self-healing-metric-push-profile"

    def ensure_metric_push_role(self):
        """Idempotent: creates (or reuses) an IAM role + instance profile
        so instances can call cloudwatch:PutMetricData without static keys."""
        import json, time as _time

        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        }

        try:
            self.iam.get_role(RoleName=self.METRIC_ROLE_NAME)
        except self.iam.exceptions.NoSuchEntityException:
            self.iam.create_role(
                RoleName=self.METRIC_ROLE_NAME,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
            )
            self.iam.put_role_policy(
                RoleName=self.METRIC_ROLE_NAME,
                PolicyName="cloudwatch-put-metric-only",
                PolicyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Effect": "Allow",
                        "Action": "cloudwatch:PutMetricData",
                        "Resource": "*",
                    }],
                }),
            )
            logger.info("Created IAM role %s", self.METRIC_ROLE_NAME)

        try:
            self.iam.get_instance_profile(InstanceProfileName=self.METRIC_PROFILE_NAME)
        except self.iam.exceptions.NoSuchEntityException:
            self.iam.create_instance_profile(InstanceProfileName=self.METRIC_PROFILE_NAME)
            self.iam.add_role_to_instance_profile(
                InstanceProfileName=self.METRIC_PROFILE_NAME,
                RoleName=self.METRIC_ROLE_NAME,
            )
            logger.info("Created instance profile %s, waiting for propagation", self.METRIC_PROFILE_NAME)
            _time.sleep(10)

        return self.METRIC_PROFILE_NAME

    # ---------------- lookup helpers (idempotency) ----------------

    def _find_by_name(self, describe_fn, resource_key, name):
        resp = describe_fn(Filters=[
            {"Name": "tag:Name", "Values": [name]},
            {"Name": "tag:Project", "Values": [config.PROJECT_NAME]},
        ])
        items = resp.get(resource_key, [])
        return items[0] if items else None

    def find_vpc(self):
        return self._find_by_name(self.client.describe_vpcs, "Vpcs", config.VPC_NAME)

    def find_subnet(self, vpc_id):
        resp = self.client.describe_subnets(Filters=[
            {"Name": "tag:Name", "Values": [config.SUBNET_NAME]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ])
        subnets = resp.get("Subnets", [])
        return subnets[0] if subnets else None

    def find_security_group(self, vpc_id):
        resp = self.client.describe_security_groups(Filters=[
            {"Name": "tag:Name", "Values": [config.SG_NAME]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ])
        sgs = resp.get("SecurityGroups", [])
        return sgs[0] if sgs else None

    def find_running_instances(self):
        resp = self.client.describe_instances(Filters=[
            {"Name": "tag:Project", "Values": [config.PROJECT_NAME]},
            {"Name": "instance-state-name", "Values": ["pending", "running"]},
        ])
        instances = []
        for res in resp.get("Reservations", []):
            instances.extend(res.get("Instances", []))
        return instances

    def _latest_ubuntu_ami(self) -> str:
        resp = self.client.describe_images(
            Owners=[config.UBUNTU_AMI_OWNER],
            Filters=[
                {"Name": "name", "Values": [config.UBUNTU_AMI_NAME_FILTER]},
                {"Name": "state", "Values": ["available"]},
            ],
        )
        images = sorted(resp["Images"], key=lambda i: i["CreationDate"], reverse=True)
        if not images:
            raise RuntimeError("Could not find a suitable Ubuntu AMI in this region.")
        return images[0]["ImageId"]

    # ---------------- create (idempotent) ----------------

    def ensure_vpc(self):
        existing = self.find_vpc()
        if existing:
            logger.info("VPC already exists: %s", existing["VpcId"])
            return existing["VpcId"]

        if self.dry_run:
            logger.info("[DRY-RUN] Would create VPC %s (%s)", config.VPC_NAME, config.VPC_CIDR)
            return "dry-run-vpc-id"

        vpc = self.client.create_vpc(
            CidrBlock=config.VPC_CIDR,
            TagSpecifications=_tag_spec("vpc", config.VPC_NAME),
        )
        vpc_id = vpc["Vpc"]["VpcId"]
        self.client.get_waiter("vpc_available").wait(VpcIds=[vpc_id])

        # Enable DNS support/hostnames (needed for public IP DNS resolution)
        self.client.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
        self.client.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

        # Internet Gateway (required for public subnet, but NOT a NAT Gateway)
        igw = self.client.create_internet_gateway(
            TagSpecifications=_tag_spec("internet-gateway", f"{config.VPC_NAME}-igw")
        )
        igw_id = igw["InternetGateway"]["InternetGatewayId"]
        self.client.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

        logger.info("Created VPC %s with IGW %s", vpc_id, igw_id)
        return vpc_id

    def ensure_subnet(self, vpc_id):
        existing = None if self.dry_run and vpc_id.startswith("dry-run") else self.find_subnet(vpc_id)
        if existing:
            logger.info("Subnet already exists: %s", existing["SubnetId"])
            return existing["SubnetId"]

        if self.dry_run:
            logger.info("[DRY-RUN] Would create subnet %s (%s)", config.SUBNET_NAME, config.SUBNET_CIDR)
            return "dry-run-subnet-id"

        subnet = self.client.create_subnet(
            VpcId=vpc_id,
            CidrBlock=config.SUBNET_CIDR,
            TagSpecifications=_tag_spec("subnet", config.SUBNET_NAME),
        )
        subnet_id = subnet["Subnet"]["SubnetId"]

        # Public subnet: auto-assign public IP on launch (no Elastic IP needed)
        self.client.modify_subnet_attribute(
            SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True}
        )

        # Route table -> IGW (makes it a public subnet)
        igws = self.client.describe_internet_gateways(Filters=[
            {"Name": "attachment.vpc-id", "Values": [vpc_id]}
        ])["InternetGateways"]
        igw_id = igws[0]["InternetGatewayId"]

        rt = self.client.create_route_table(
            VpcId=vpc_id,
            TagSpecifications=_tag_spec("route-table", f"{config.PROJECT_NAME}-rt"),
        )
        rt_id = rt["RouteTable"]["RouteTableId"]
        self.client.create_route(
            RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id
        )
        self.client.associate_route_table(RouteTableId=rt_id, SubnetId=subnet_id)

        logger.info("Created subnet %s (public, routed to IGW %s)", subnet_id, igw_id)
        return subnet_id

    def ensure_security_group(self, vpc_id):
        existing = None if self.dry_run and vpc_id.startswith("dry-run") else self.find_security_group(vpc_id)
        if existing:
            logger.info("Security group already exists: %s", existing["GroupId"])
            return existing["GroupId"]

        if self.dry_run:
            logger.info("[DRY-RUN] Would create security group %s", config.SG_NAME)
            return "dry-run-sg-id"

        sg = self.client.create_security_group(
            GroupName=config.SG_NAME,
            Description="Self-healing platform - SSH + HTTP + health check port",
            VpcId=vpc_id,
            TagSpecifications=_tag_spec("security-group", config.SG_NAME),
        )
        sg_id = sg["GroupId"]

        self.client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                 "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}]},
                {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                 "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTP"}]},
                {"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080,
                 "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "app health endpoint"}]},
            ],
        )
        logger.info("Created security group %s", sg_id)
        return sg_id

    def ensure_instances(self, subnet_id, sg_id, key_name, count: int = None):
        count = count or config.INSTANCE_COUNT
        existing = self.find_running_instances()
        if len(existing) >= count:
            logger.info("%d instance(s) already running, skipping create.", len(existing))
            return [i["InstanceId"] for i in existing]

        to_create = count - len(existing)
        if self.dry_run:
            logger.info("[DRY-RUN] Would launch %d instance(s) of type %s",
                        to_create, self.instance_type)
            return [f"dry-run-instance-{i}" for i in range(to_create)]

        ami_id = self._latest_ubuntu_ami()
        profile_name = self.ensure_metric_push_role()
        new_ids = []
        for i in range(to_create):
            name = f"{config.INSTANCE_NAME_PREFIX}-{len(existing) + i + 1}"
            resp = self.client.run_instances(
                ImageId=ami_id,
                InstanceType=self.instance_type,
                KeyName=key_name,
                MinCount=1,
                MaxCount=1,
                SubnetId=subnet_id,
                SecurityGroupIds=[sg_id],
                TagSpecifications=_tag_spec("instance", name),
                IamInstanceProfile={"Name": profile_name},
            )
            iid = resp["Instances"][0]["InstanceId"]
            new_ids.append(iid)
            logger.info("Launched instance %s (%s)", iid, name)

        self.client.get_waiter("instance_running").wait(InstanceIds=new_ids)
        return [i["InstanceId"] for i in existing] + new_ids

    def provision_all(self, key_name: str, count: int = None):
        """Full idempotent provisioning flow. Used by CLI and by the self-healing daemon."""
        vpc_id = self.ensure_vpc()
        subnet_id = self.ensure_subnet(vpc_id)
        sg_id = self.ensure_security_group(vpc_id)
        instance_ids = self.ensure_instances(subnet_id, sg_id, key_name, count)
        return {
            "vpc_id": vpc_id,
            "subnet_id": subnet_id,
            "sg_id": sg_id,
            "instance_ids": instance_ids,
        }

    # ---------------- destroy (correct dependency order) ----------------

    def destroy_all(self):
        vpc = self.find_vpc()
        if not vpc:
            logger.info("No VPC found for this project. Nothing to destroy.")
            return
        vpc_id = vpc["VpcId"]

        # 1. Terminate instances
        instances = self.find_running_instances()
        if instances:
            ids = [i["InstanceId"] for i in instances]
            if self.dry_run:
                logger.info("[DRY-RUN] Would terminate instances: %s", ids)
            else:
                self.client.terminate_instances(InstanceIds=ids)
                self.client.get_waiter("instance_terminated").wait(InstanceIds=ids)
                logger.info("Terminated instances: %s", ids)

        # 2. Delete security group
        sg = self.find_security_group(vpc_id)
        if sg:
            if self.dry_run:
                logger.info("[DRY-RUN] Would delete security group %s", sg["GroupId"])
            else:
                self._retry_delete(self.client.delete_security_group, GroupId=sg["GroupId"])
                logger.info("Deleted security group %s", sg["GroupId"])

        # 3. Delete subnet
        subnet = self.find_subnet(vpc_id)
        if subnet:
            if self.dry_run:
                logger.info("[DRY-RUN] Would delete subnet %s", subnet["SubnetId"])
            else:
                self.client.delete_subnet(SubnetId=subnet["SubnetId"])
                logger.info("Deleted subnet %s", subnet["SubnetId"])

        # 4. Detach + delete IGW, delete route table, then VPC
        if not self.dry_run:
            igws = self.client.describe_internet_gateways(Filters=[
                {"Name": "attachment.vpc-id", "Values": [vpc_id]}
            ])["InternetGateways"]
            for igw in igws:
                igw_id = igw["InternetGatewayId"]
                self.client.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
                self.client.delete_internet_gateway(InternetGatewayId=igw_id)
                logger.info("Deleted IGW %s", igw_id)

            rts = self.client.describe_route_tables(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "tag:Project", "Values": [config.PROJECT_NAME]},
            ])["RouteTables"]
            for rt in rts:
                self.client.delete_route_table(RouteTableId=rt["RouteTableId"])
                logger.info("Deleted route table %s", rt["RouteTableId"])

            self.client.delete_vpc(VpcId=vpc_id)
            logger.info("Deleted VPC %s", vpc_id)
        else:
            logger.info("[DRY-RUN] Would delete IGW, route table, and VPC %s", vpc_id)

    @staticmethod
    def _retry_delete(fn, retries=5, delay=3, **kwargs):
        """SG deletion can fail transiently if instances just terminated (ENI cleanup lag)."""
        for attempt in range(retries):
            try:
                fn(**kwargs)
                return
            except ClientError as e:
                if attempt == retries - 1:
                    raise
                logger.warning("Retrying delete (%s)... attempt %d", e, attempt + 1)
                time.sleep(delay)
