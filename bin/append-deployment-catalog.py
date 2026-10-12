#!/usr/bin/env python3
# bin/append-deployment-catalog.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Atomically record one cloud instance launch in the deployment catalog."""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from catalog_common import (  # noqa: E402
    exclusive_lock,
    load_or_init_catalog,
    require_missing_fields,
    require_nonempty_string,
    validate_string_list,
    validate_timestamp,
    write_catalog,
)

COMMON_REQUIRED = {
    "vm_name", "target_platform", "status", "launched_at",
}

AWS_REQUIRED = {
    "ami_id", "import_task_id", "instance_id", "instance_type",
    "aws_region", "vpc_subnet_id",
}

OS_REQUIRED = {
    "openstack_server_id", "openstack_image_ids", "openstack_project",
    "openstack_flavor", "openstack_network", "openstack_data_volume_ids",
}

GCP_REQUIRED = {
    "gcp_instance_name", "gcp_project", "gcp_zone", "gcp_machine_type",
    "gcp_boot_image_name", "gcp_data_disk_names",
}

def validate(entry):
    require_missing_fields(entry, COMMON_REQUIRED)
    if entry["target_platform"] == "aws":
        target_required = AWS_REQUIRED
    elif entry["target_platform"] == "openstack":
        target_required = OS_REQUIRED
    elif entry["target_platform"] == "gcp":
        target_required = GCP_REQUIRED
    else:
        raise ValueError(
            "unsupported target_platform: " + str(entry["target_platform"])
        )
    require_missing_fields(entry, target_required, entry["target_platform"])

    for name in COMMON_REQUIRED:
        require_nonempty_string(entry, name)
    validate_timestamp(entry["launched_at"], "launched_at")

    target = entry["target_platform"]
    if target == "aws" and entry.get("aws_account_id") is not None:
        require_nonempty_string(entry, "aws_account_id")
    if target == "openstack":
        image_ids = validate_string_list(
            entry, "openstack_image_ids", allow_empty=False
        )
        validate_string_list(entry, "openstack_data_volume_ids", allow_empty=True)
        require_nonempty_string(entry, "openstack_server_id")
        require_nonempty_string(entry, "openstack_project")
        project_id = entry.get("openstack_project_id")
        if project_id is not None:
            require_nonempty_string(entry, "openstack_project_id")
        region = entry.get("openstack_region")
        if region is not None and (not isinstance(region, str) or not region.strip()):
            raise ValueError(
                "openstack_region must be a non-empty string when present")
        require_nonempty_string(entry, "openstack_flavor")
        if not isinstance(entry["openstack_network"], str):
            raise ValueError("openstack_network must be a string")
        network_id = entry.get("openstack_network_id")
        if network_id is not None and not isinstance(network_id, str):
            raise ValueError("openstack_network_id must be a string")
        network_ids = entry.get("openstack_network_ids")
        if network_ids is not None:
            if not isinstance(network_ids, list) or not all(
                isinstance(value, str) and value.strip() for value in network_ids
            ):
                raise ValueError(
                    "openstack_network_ids must be a list of non-empty strings"
                )
        nics = entry.get("openstack_nics")
        if nics is not None and not isinstance(nics, list):
            raise ValueError("openstack_nics must be a list")
        port_ids = entry.get("openstack_port_ids")
        if port_ids is not None:
            if not isinstance(port_ids, list) or not all(
                isinstance(value, str) and value.strip() for value in port_ids
            ):
                raise ValueError(
                    "openstack_port_ids must be a list of non-empty strings"
                )
        has_named_network = bool(entry["openstack_network"].strip()) or bool(
            (network_id or "").strip()
        )
        has_network_ids = bool(network_ids)
        has_port_nics = bool(port_ids) or bool(
            nics
            and any(
                isinstance(nic, dict) and str(nic.get("port-id", "")).strip()
                for nic in nics
            )
        )
        if not (has_named_network or has_network_ids or has_port_nics):
            raise ValueError(
                "openstack deployment requires a network name/id, "
                "openstack_network_ids, or port-backed openstack_nics/"
                "openstack_port_ids"
            )
        if "openstack_security_group_ids" in entry:
            validate_string_list(
                entry, "openstack_security_group_ids", allow_empty=True)
        if not image_ids:
            raise ValueError("openstack_image_ids must not be empty")

    elif target == "gcp":
        for name in GCP_REQUIRED - {"gcp_data_disk_names"}:
            require_nonempty_string(entry, name)
        validate_string_list(entry, "gcp_data_disk_names", allow_empty=True)

    else:
        for name in AWS_REQUIRED:
            require_nonempty_string(entry, name)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--entry", required=True, type=pathlib.Path)
    args = parser.parse_args()

    entry = json.loads(args.entry.read_text(encoding="utf-8"))
    validate(entry)
    path = pathlib.Path(args.catalog)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(path):
        catalog = load_or_init_catalog(path, "deployments")

        def deployment_identity(item):
            target = item.get("target_platform")
            if target == "gcp":
                resource = item.get("gcp_instance_name")
                scope = (item.get("gcp_project", ""), item.get("gcp_zone", ""))
            elif target == "aws":
                resource = item.get("instance_id")
                scope = (item.get("aws_account_id", ""), item.get("aws_region", ""))
            else:
                resource = item.get("openstack_server_id")
                scope = (item.get("openstack_project_id", item.get("openstack_project", "")),
                         item.get("openstack_region", ""))
            return target, resource, tuple(str(value) for value in scope)

        # Keep deployment history. A target-specific resource ID identifies
        # the exact launch to replace on an idempotent catalog append.
        entry_identity = deployment_identity(entry)
        catalog["deployments"] = [
            old for old in catalog["deployments"]
            if deployment_identity(old) != entry_identity
        ]
        catalog["deployments"].append(entry)
        write_catalog(path, catalog, "deployments")

if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"append-deployment-catalog: {error}")
