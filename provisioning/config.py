"""
Central config for the provisioning layer.
Free-tier guardrails live here so they're enforced in exactly one place.
"""

import boto3

# ---- Free-tier eligible instance types by region ----
# Most regions: t2.micro. ap-south-1 (Mumbai) and a few others: t3.micro.
FREE_TIER_INSTANCE_MAP = {
    "ap-south-1": "t3.micro",
}
DEFAULT_INSTANCE_TYPE = "t2.micro"

ALLOWED_INSTANCE_TYPES = {"t2.micro", "t3.micro"}

# ---- Project tagging (enforced everywhere, not applied manually) ----
PROJECT_NAME = "self-healing-platform"
OWNER = "sonujha78"
ENVIRONMENT = "dev"

TAGS = {
    "Project": PROJECT_NAME,
    "Owner": OWNER,
    "Environment": ENVIRONMENT,
}

# ---- Network config ----
VPC_CIDR = "10.20.0.0/16"
SUBNET_CIDR = "10.20.1.0/24"
INSTANCE_COUNT = 2  # keep at 2-3 per free tier plan

# ---- Naming convention (used for idempotency lookups by tag:Name) ----
VPC_NAME = f"{PROJECT_NAME}-vpc"
SUBNET_NAME = f"{PROJECT_NAME}-subnet"
SG_NAME = f"{PROJECT_NAME}-sg"
INSTANCE_NAME_PREFIX = f"{PROJECT_NAME}-node"

# ---- AMI: latest Ubuntu 22.04 LTS (looked up dynamically, not hardcoded) ----
UBUNTU_AMI_OWNER = "099720109477"  # Canonical
UBUNTU_AMI_NAME_FILTER = "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"


def get_region() -> str:
    session = boto3.session.Session()
    region = session.region_name
    if not region:
        raise RuntimeError(
            "No AWS region configured. Run `aws configure` and set a default region."
        )
    return region


def get_instance_type_for_region(region: str) -> str:
    return FREE_TIER_INSTANCE_MAP.get(region, DEFAULT_INSTANCE_TYPE)


def validate_instance_type(instance_type: str) -> None:
    if instance_type not in ALLOWED_INSTANCE_TYPES:
        raise ValueError(
            f"Refusing to use '{instance_type}'. Only {ALLOWED_INSTANCE_TYPES} "
            f"are allowed under this project's free-tier policy."
        )
