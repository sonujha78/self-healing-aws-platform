"""
Click-based CLI — replaces what Terraform would do, hand-written with boto3.

Usage:
    python -m provisioning.cli up --key-name my-keypair
    python -m provisioning.cli up --dry-run
    python -m provisioning.cli destroy
    python -m provisioning.cli status
"""

import logging
import click

from provisioning.aws_manager import AWSManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


@click.group()
def cli():
    """Self-Healing Platform — Infra Provisioning CLI (pure boto3, no Terraform)."""
    pass


@cli.command()
@click.option("--key-name", required=True, help="Existing EC2 KeyPair name for SSH access.")
@click.option("--count", default=None, type=int, help="Number of instances (default from config).")
@click.option("--dry-run", is_flag=True, help="Print planned changes without calling AWS.")
def up(key_name, count, dry_run):
    """Provision VPC, subnet, security group, and EC2 instances (idempotent)."""
    manager = AWSManager(dry_run=dry_run)
    result = manager.provision_all(key_name=key_name, count=count)
    click.echo("\n--- Provisioning result ---")
    for k, v in result.items():
        click.echo(f"{k}: {v}")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Print planned deletions without calling AWS.")
def destroy(dry_run):
    """Tear down everything in correct dependency order."""
    manager = AWSManager(dry_run=dry_run)
    manager.destroy_all()
    click.echo("Destroy complete." if not dry_run else "[DRY-RUN] Destroy plan printed above.")


@cli.command()
def status():
    """Show currently running project instances."""
    manager = AWSManager()
    instances = manager.find_running_instances()
    if not instances:
        click.echo("No running instances for this project.")
        return
    for i in instances:
        name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), "?")
        click.echo(f"{i['InstanceId']}  {name}  {i['State']['Name']}  {i.get('PublicIpAddress', '-')}")


if __name__ == "__main__":
    cli()
