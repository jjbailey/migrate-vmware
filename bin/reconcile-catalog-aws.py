#!/usr/bin/env python3
# bin/reconcile-catalog-aws.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Remove AWS catalog entries when their resources are gone."""

import argparse
import json
from pathlib import Path

from catalog_common import read_snapshot, remove_deployment_entries, remove_migration_entries

import boto3
from botocore.exceptions import ClientError

NOT_FOUND_CODES = {
    "InvalidAMIID.NotFound",
    "InvalidAMIID.Malformed",
    "InvalidInstanceID.NotFound",
    "InvalidInstanceID.Malformed",
}

def catalog_scope(migration_catalog, deployment_catalog, account_id=None):
    regions, accounts = set(), set()
    for catalog, list_key in ((migration_catalog, "migrations"), (deployment_catalog, "deployments")):
        for entry in catalog[list_key]:
            if entry.get("target_platform") != "aws":
                continue
            # With --account-id, another account's rows are out of scope, so a
            # malformed one there must not abort this run.
            if account_id and not account_matches(entry, account_id):
                continue
            region = entry.get("aws_region")
            if not isinstance(region, str) or not region.strip():
                raise ValueError(
                    f"AWS {list_key[:-1]} {entry.get('vm_name', '<unknown>')} has no aws_region")
            regions.add(region)
            account = entry.get("aws_account_id")
            if account:
                accounts.add(str(account))
    return regions, accounts

def account_matches(entry, account_id):
    # Catalog rows written before account metadata are legacy rows. They are
    # usable in the authenticated account, but new rows are exact-scoped.
    return (entry.get("target_platform") != "aws"
            or not entry.get("aws_account_id")
            or str(entry["aws_account_id"]) == str(account_id))

def resource_missing(client, operation, identifier, cache):
    key = (operation, identifier)
    if key in cache:
        return cache[key]
    try:
        if operation == "image":
            result = client.describe_images(
                ImageIds=[identifier]).get("Images", [])
            missing = not result
        else:
            result = client.describe_instances(InstanceIds=[identifier])
            instances = [instance for reservation in result.get(
                "Reservations", []) for instance in reservation.get("Instances", [])]
            missing = not instances or instances[0].get(
                "State", {}).get("Name") == "terminated"
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code in NOT_FOUND_CODES:
            missing = True
        else:
            raise
    cache[key] = missing
    return missing

def find_missing_resources(session, migration_catalog, deployment_catalog, account_id, requested_region=None):
    clients = {}
    image_cache, instance_cache = {}, {}

    def client_for(region):
        if region not in clients:
            clients[region] = session.client("ec2", region_name=region)
        return clients[region]

    stale_migrations, stale_deployments = [], []
    for entry in migration_catalog["migrations"]:
        if entry.get("target_platform") != "aws" or entry.get("status") != "completed" or not account_matches(entry, account_id):
            continue
        region, ami_id = entry.get("aws_region"), entry.get("ami_id")
        if requested_region and region != requested_region:
            continue
        if not isinstance(ami_id, str) or not ami_id.strip():
            raise ValueError(
                f"AWS migration {entry.get('clone_name', '<unknown>')} has no ami_id")
        if resource_missing(client_for(region), "image", ami_id, image_cache):
            stale_migrations.append((entry, ami_id))

    for entry in deployment_catalog["deployments"]:
        if entry.get("target_platform") != "aws" or entry.get("status") == "retired" or not account_matches(entry, account_id):
            continue
        region, instance_id = entry.get("aws_region"), entry.get("instance_id")
        if requested_region and region != requested_region:
            continue
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise ValueError(
                f"AWS deployment for {entry.get('vm_name', '<unknown>')} has no instance_id")
        if resource_missing(client_for(region), "instance", instance_id, instance_cache):
            stale_deployments.append((entry, instance_id))
    return stale_migrations, stale_deployments

def candidate_count(migration_catalog, deployment_catalog, account_id, requested_region=None):
    migrations = sum(1 for entry in migration_catalog["migrations"]
                     if entry.get("target_platform") == "aws"
                     and entry.get("status") == "completed"
                     and account_matches(entry, account_id)
                     and (not requested_region or entry.get("aws_region") == requested_region))
    deployments = sum(1 for entry in deployment_catalog["deployments"]
                      if entry.get("target_platform") == "aws"
                      and entry.get("status") != "retired"
                      and account_matches(entry, account_id)
                      and (not requested_region or entry.get("aws_region") == requested_region))
    return migrations + deployments

def main():
    repo_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-catalog", type=Path,
                        default=repo_dir / "catalogs/migration-catalog.json")
    parser.add_argument("--deployment-catalog", type=Path,
                        default=repo_dir / "catalogs/deployment-catalog.json")
    parser.add_argument(
        "--region", help="only reconcile AWS entries in this region")
    parser.add_argument("--profile", help="boto3/AWS CLI profile")
    parser.add_argument(
        "--project-id", "--account-id", dest="account_id",
        help="AWS account ID; verify and reconcile only this account")
    parser.add_argument("--dry-run", action="store_true",
                        help="report stale entries without writing either catalog")
    parser.add_argument("--force", action="store_true",
                        help="allow removal of more than half of the scoped entries")
    args = parser.parse_args()

    migration_catalog = read_snapshot(args.migration_catalog, "migrations")
    deployment_catalog = read_snapshot(args.deployment_catalog, "deployments")
    regions, catalog_accounts = catalog_scope(
        migration_catalog, deployment_catalog, args.account_id)
    if args.region and args.region not in regions:
        raise ValueError(
            f"catalog contains no AWS entries for region {args.region}")

    session = boto3.Session(
        profile_name=args.profile) if args.profile else boto3.Session()
    authenticated_account = session.client("sts").get_caller_identity()["Account"]
    if args.account_id and str(authenticated_account) != str(args.account_id):
        raise ValueError(
            f"authenticated AWS account {authenticated_account} does not match --account-id {args.account_id}")
    if args.account_id:
        account_id = str(args.account_id)
    elif len(catalog_accounts) == 1:
        account_id = next(iter(catalog_accounts))
    elif not catalog_accounts:
        account_id = str(authenticated_account)
    else:
        raise ValueError(
            "catalog contains multiple AWS accounts; provide --account-id")
    if account_id != str(authenticated_account):
        raise ValueError(
            f"authenticated AWS account {authenticated_account} does not match catalog account {account_id}")
    print(f"AWS scope: account_id={account_id}, regions={', '.join(sorted(regions if not args.region else {args.region}))}")

    stale_migrations, stale_deployments = find_missing_resources(
        session, migration_catalog, deployment_catalog, account_id, args.region)
    retired_migrations = [entry for entry in migration_catalog["migrations"]
                          if entry.get("target_platform") == "aws"
                          and entry.get("status") == "retired"
                          and account_matches(entry, account_id)
                          and (not args.region or entry.get("aws_region") == args.region)]
    retired_deployments = [entry for entry in deployment_catalog["deployments"]
                           if entry.get("target_platform") == "aws"
                           and entry.get("status") == "retired"
                           and account_matches(entry, account_id)
                           and (not args.region or entry.get("aws_region") == args.region)]

    for entry, ami_id in stale_migrations:
        print(
            f"removing migration {entry.get('vm_name', '<unknown>')} {entry.get('import_task_id', '<unknown>')}: missing AMI {ami_id} in {entry.get('aws_region', '<unknown>')}")
    for entry, instance_id in stale_deployments:
        print(
            f"removing deployment {entry.get('vm_name', '<unknown>')} {instance_id}: missing or terminated EC2 instance in {entry.get('aws_region', '<unknown>')}")
    for entry in retired_migrations:
        print(
            f"removing previously retired migration {entry.get('vm_name', '<unknown>')} {entry.get('import_task_id', '<unknown>')}")
    for entry in retired_deployments:
        print(
            f"removing previously retired deployment {entry.get('vm_name', '<unknown>')} {entry.get('instance_id', '<unknown>')}")

    stale_total = len(stale_migrations) + len(stale_deployments)
    total = stale_total + len(retired_migrations) + len(retired_deployments)
    if not total:
        print("no stale AWS catalog entries found")
        return 0
    if args.dry_run:
        print(f"{total} entries would be removed (--dry-run, catalogs not written)")
        return 0
    scope_total = candidate_count(
        migration_catalog, deployment_catalog, account_id, args.region)
    if stale_total and scope_total and stale_total * 2 > scope_total and not args.force:
        raise ValueError(
            f"refusing to remove {stale_total} of {scope_total} scoped entries; inspect --dry-run and rerun with --force if this is intentional")

    migration_changed = remove_migration_entries(
        args.migration_catalog, stale_migrations, retired_migrations, "aws", "import_task_id", args.region, account_id)
    deployment_changed = remove_deployment_entries(
        args.deployment_catalog, stale_deployments, retired_deployments, "aws", "instance_id", args.region, account_id)
    print(
        f"removed {migration_changed} migration and {deployment_changed} deployment entries")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, ClientError) as error:
        raise SystemExit(f"reconcile-catalog-aws: {error}")
