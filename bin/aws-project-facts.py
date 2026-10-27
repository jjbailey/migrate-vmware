#!/usr/bin/env python3
# bin/aws-project-facts.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""List the EC2 subnets, security groups, and keypairs one AWS account can use.

Credentials come from vaults/aws.yml (aws_projects, matched by vault key,
account ID, or profile, the same way select-project.yml does), or from the
standard boto3 chain (env vars, ~/.aws, instance profile). The region comes
from the vault entry unless --region overrides it.

When --subnet is given, the subnet's VPC is looked up first and the
security-group listing is filtered to that VPC, since a launch's groups
must live in the same VPC as its subnet. When the subnet cannot be
described, the listing falls back to all VPCs rather than failing, and
vpc_id keeps rows distinguishable.

With --subnets, subnets are listed instead (id, Name tag, CIDR, AZ, VPC,
free IPs), for the dialog's subnet picker. With --keypairs, EC2 keypairs
are listed instead, for the dialog's key picker.

Output is a table by default, or JSON with --json for scripting
(launch-aws-dialog.sh uses this for its subnet, keypair, and multi-select
security-group pickers).
"""

import argparse
import json
import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from vault_common import read_vault  # noqa: E402

def select_vault_entry(vault, requested):
    data = read_vault(vault)
    projects = {str(key): value for key, value in (
        data.get("aws_projects") or {}).items()}
    if requested:
        for key, entry in projects.items():
            entry = entry or {}
            if requested in {
                key,
                str(entry.get("aws_account_id", "")
                    or entry.get("account_id", "")),
                str(entry.get("aws_profile", "")
                    or entry.get("profile", "")),
            }:
                return key, entry
        if not projects and data.get("aws_region"):
            account = str(data.get("aws_account_id", "")
                           or data.get("account_id", ""))
            if requested in {account, "legacy"} or not account:
                return "legacy", data
        raise SystemExit(
            f"AWS project '{requested}' matches no vault key, account ID, "
            f"or profile in {vault}. "
            f"Available: {', '.join(projects) or '<none>'}"
        )
    if not projects and data.get("aws_region"):
        return "legacy", data
    if len(projects) == 1:
        return next(iter(projects.items()))
    raise SystemExit(
        f"{vault} holds several projects; name one as the PROJECT argument "
        f"({', '.join(projects) or '<none>'})"
    )

def make_session(entry, profile_override=None):
    import boto3

    profile = (profile_override
               or str(entry.get("aws_profile", "")
                      or entry.get("profile", "")).strip())
    if profile:
        return boto3.Session(profile_name=profile)
    key_id = str(entry.get("aws_access_key_id", "") or "").strip()
    secret = str(entry.get("aws_secret_access_key", "") or "").strip()
    token = str(entry.get("aws_session_token", "") or "").strip()
    if key_id and secret:
        kwargs = {"aws_access_key_id": key_id,
                  "aws_secret_access_key": secret}
        if token:
            kwargs["aws_session_token"] = token
        return boto3.Session(**kwargs)
    return boto3.Session()

def gather_security_groups(client, vpc_id=None):
    paginator = client.get_paginator("describe_security_groups")
    if vpc_id:
        pages = paginator.paginate(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    else:
        pages = paginator.paginate()
    groups = []
    for page in pages:
        for group in page.get("SecurityGroups", []):
            ingress = len(group.get("IpPermissions", []))
            egress = len(group.get("IpPermissionsEgress", []))
            groups.append({
                "id": group.get("GroupId", ""),
                "name": group.get("GroupName", ""),
                "description": group.get("Description", ""),
                "vpc_id": group.get("VpcId", ""),
                "rules": f"{ingress} in/{egress} out",
            })
    groups.sort(key=lambda group: (
        group["name"] != "default", group["name"], group["id"]))
    return groups

def gather_subnets(client):
    paginator = client.get_paginator("describe_subnets")
    subnets = []
    for page in paginator.paginate():
        for subnet in page.get("Subnets", []):
            name = ""
            for tag in subnet.get("Tags", []) or []:
                if tag.get("Key") == "Name":
                    name = str(tag.get("Value") or "")
                    break
            subnets.append({
                "id": subnet.get("SubnetId", ""),
                "name": name,
                "cidr": subnet.get("CidrBlock", ""),
                "az": subnet.get("AvailabilityZone", ""),
                "vpc_id": subnet.get("VpcId", ""),
                "available_ips": subnet.get("AvailableIpAddressCount", 0),
                "default": bool(subnet.get("DefaultForAz", False)),
                "state": subnet.get("State", ""),
            })
    subnets.sort(key=lambda item: (
        item["name"] or "~", item["cidr"], item["id"]))
    return subnets

def gather_keypairs(client):
    # describe_key_pairs has no paginator model in botocore, unlike
    # describe_subnets / describe_security_groups, and the result is a
    # small list, so one call is enough.
    page = client.describe_key_pairs()
    pairs = []
    for pair in page.get("KeyPairs", []):
        pairs.append({
            "name": pair.get("KeyName", ""),
            "type": pair.get("KeyType", ""),
            "fingerprint": pair.get("KeyFingerprint", ""),
        })
    pairs.sort(key=lambda pair: pair["name"])
    return pairs

def print_table(groups, vpc_id=None):
    scope = f" (VPC {vpc_id})" if vpc_id else ""
    print(f"Security groups{scope}: {len(groups)}")
    if not groups:
        return
    widths = {"id": 2, "name": 4, "vpc": 3, "rules": 5}
    for group in groups:
        widths["id"] = max(widths["id"], len(group["id"]))
        widths["name"] = max(widths["name"], len(group["name"]))
        widths["vpc"] = max(widths["vpc"], len(group["vpc_id"]))
        widths["rules"] = max(widths["rules"], len(group["rules"]))
    print(f"  {'ID':<{widths['id']}}  {'NAME':<{widths['name']}}  "
          f"{'VPC':<{widths['vpc']}}  {'RULES':<{widths['rules']}}  "
          "DESCRIPTION")
    for group in groups:
        print(f"  {group['id']:<{widths['id']}}  "
              f"{group['name']:<{widths['name']}}  "
              f"{group['vpc_id']:<{widths['vpc']}}  "
              f"{group['rules']:<{widths['rules']}}  "
              f"{group['description']}")

def print_subnets_table(subnets):
    print(f"Subnets: {len(subnets)}")
    if not subnets:
        return
    widths = {"id": 2, "name": 4, "cidr": 4, "az": 2, "vpc": 3}
    for item in subnets:
        widths["id"] = max(widths["id"], len(item["id"]))
        widths["name"] = max(widths["name"], len(item["name"] or "-"))
        widths["cidr"] = max(widths["cidr"], len(item["cidr"]))
        widths["az"] = max(widths["az"], len(item["az"]))
        widths["vpc"] = max(widths["vpc"], len(item["vpc_id"]))
    print(f"  {'ID':<{widths['id']}}  {'NAME':<{widths['name']}}  "
          f"{'CIDR':<{widths['cidr']}}  {'AZ':<{widths['az']}}  "
          f"{'VPC':<{widths['vpc']}}  FREE  DEFAULT STATE")
    for item in subnets:
        name = item["name"] or "-"
        print(f"  {item['id']:<{widths['id']}}  "
              f"{name:<{widths['name']}}  "
              f"{item['cidr']:<{widths['cidr']}}  "
              f"{item['az']:<{widths['az']}}  "
              f"{item['vpc_id']:<{widths['vpc']}}  "
              f"{item['available_ips']:>4}  "
              f"{'yes' if item['default'] else 'no':<7} "
              f"{item['state']}")

def print_keypairs_table(pairs):
    print(f"Keypairs: {len(pairs)}")
    if not pairs:
        return
    widths = {"name": 4, "type": 4, "fp": 11}
    for pair in pairs:
        widths["name"] = max(widths["name"], len(pair["name"]))
        widths["type"] = max(widths["type"], len(pair["type"]))
        widths["fp"] = max(widths["fp"], len(pair["fingerprint"]))
    print(f"  {'NAME':<{widths['name']}}  {'TYPE':<{widths['type']}}  FINGERPRINT")
    for pair in pairs:
        print(f"  {pair['name']:<{widths['name']}}  "
              f"{pair['type']:<{widths['type']}}  "
              f"{pair['fingerprint']}")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", nargs="?",
                        help="vault key, account ID, or profile "
                             "(omit for a single-entry vault)")
    parser.add_argument("--vault", default=str(REPO_DIR / "vaults/aws.yml"))
    parser.add_argument("--subnet",
                        help="filter groups to this subnet's VPC (e.g. subnet-abc)")
    parser.add_argument("--subnets", dest="list_subnets", action="store_true",
                        help="list subnets instead of security groups")
    parser.add_argument("--keypairs", dest="list_keypairs", action="store_true",
                        help="list keypairs instead of security groups")
    parser.add_argument("--region",
                        help="override the vault entry's aws_region")
    parser.add_argument("--profile", help="boto3 profile override")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="machine-readable output for scripting")
    args = parser.parse_args()

    key, entry = select_vault_entry(args.vault, args.project)
    entry = entry or {}
    region = (args.region
              or str(entry.get("aws_region", "") or "").strip()
              or os.environ.get("AWS_REGION", "")
              or os.environ.get("AWS_DEFAULT_REGION", ""))
    if not region:
        raise SystemExit(
            f"no region: vault entry '{key}' has no aws_region and --region "
            "was not given")

    try:
        import botocore.exceptions
    except ImportError:
        raise SystemExit("boto3/botocore is required (see README)")

    session = make_session(entry, args.profile)
    client = session.client("ec2", region_name=region)

    if args.list_subnets:
        try:
            subnets = gather_subnets(client)
        except botocore.exceptions.ClientError as error:
            raise SystemExit(f"could not list subnets: {error}")
        if args.as_json:
            json.dump({"project": key, "region": region,
                       "subnets": subnets},
                      sys.stdout, indent=2, default=str)
            print()
            return 0
        print(f"AWS project: {key}, region {region}")
        print_subnets_table(subnets)
        return 0

    if args.list_keypairs:
        try:
            pairs = gather_keypairs(client)
        except botocore.exceptions.ClientError as error:
            raise SystemExit(f"could not list keypairs: {error}")
        if args.as_json:
            json.dump({"project": key, "region": region,
                       "keypairs": pairs},
                      sys.stdout, indent=2, default=str)
            print()
            return 0
        print(f"AWS project: {key}, region {region}")
        print_keypairs_table(pairs)
        return 0

    vpc_id = ""
    if args.subnet:
        try:
            subnets = client.describe_subnets(
                SubnetIds=[args.subnet]).get("Subnets", [])
            if subnets:
                vpc_id = subnets[0].get("VpcId", "")
        except botocore.exceptions.ClientError:
            vpc_id = ""

    try:
        groups = gather_security_groups(client, vpc_id or None)
    except botocore.exceptions.ClientError as error:
        raise SystemExit(f"could not list security groups: {error}")

    if args.as_json:
        json.dump({"project": key, "region": region, "vpc_id": vpc_id,
                   "subnet": args.subnet or "",
                   "security_groups": groups},
                  sys.stdout, indent=2, default=str)
        print()
        return 0

    print(f"AWS project: {key}, region {region}")
    print_table(groups, vpc_id)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(f"aws-project-facts: {error}")

