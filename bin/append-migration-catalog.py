#!/usr/bin/env python3
# bin/append-migration-catalog.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Atomically record one VM migration in the migration catalog."""

import argparse
import json
import pathlib
import sys
from urllib.parse import urlparse

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
    "vm_name", "source_vcenter", "source_datacenter", "clone_name",
    "snapshot_name", "target_platform", "artifact_type", "artifact_manifest",
    "status", "migrated_at",
}

AWS_REQUIRED = {
    "s3_bucket", "s3_key", "aws_region", "ami_id", "ami_name", "import_task_id",
}

OS_REQUIRED = {
    "openstack_image_ids", "openstack_image_names", "openstack_project",
}

GCP_REQUIRED = {
    "gcp_project", "gcp_import_location", "gcs_bucket", "gcs_prefix",
    "gcp_boot_image_name", "gcp_boot_image_selflink",
}

def migration_identity(entry):
    """Identity for an idempotent retry, including target project scope."""
    target = entry.get("target_platform")
    if target == "aws":
        scope = entry.get("aws_account_id", "")
    elif target == "openstack":
        scope = entry.get("openstack_project_id", entry.get("openstack_project", ""))
    elif target == "gcp":
        scope = entry.get("gcp_project", "")
    else:
        scope = ""
    resource = entry.get("import_task_id", entry.get("clone_name"))
    return target, resource, str(scope)

def validate_disk_order(disks):
    """Disk order is boot order: index i is the i-th disk, and only 0 boots."""
    for position, disk in enumerate(disks):
        if not isinstance(disk, dict) or disk.get("index") != position:
            raise ValueError(
                f"artifact_manifest.disks[{position}] must carry index {position}")
        if bool(disk.get("boot_candidate")) != (position == 0):
            raise ValueError(
                "artifact_manifest.disks: only disk index 0 may be the boot disk")

def validate_image_order(names, disks, label):
    """Image names are <prefix>-disk-<index>; they must follow the manifest."""
    for name, disk in zip(names, disks):
        if not name.endswith(f"-disk-{disk['index']}"):
            raise ValueError(
                f"{label} {name!r} is out of manifest disk order "
                f"(expected a name ending -disk-{disk['index']})")

def validate(entry):
    require_missing_fields(entry, COMMON_REQUIRED)
    target = entry["target_platform"]
    if target == "aws":
        target_required = AWS_REQUIRED
    elif target == "openstack":
        target_required = OS_REQUIRED
    elif target == "gcp":
        target_required = GCP_REQUIRED
    else:
        raise ValueError("unsupported target_platform: " + str(target))
    require_missing_fields(entry, target_required, target)

    for name in COMMON_REQUIRED - {"artifact_manifest"}:
        require_nonempty_string(entry, name)
    validate_timestamp(entry["migrated_at"], "migrated_at")

    manifest = entry["artifact_manifest"]
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 1:
        raise ValueError("artifact_manifest must be a version 1 object")
    disks = manifest.get("disks")
    if not isinstance(disks, list) or not disks:
        raise ValueError("artifact_manifest.disks must be a non-empty list")
    if manifest.get("disk_count") != len(disks):
        raise ValueError("artifact_manifest.disk_count does not match disks")

    validate_disk_order(disks)

    for name in target_required:
        if name in {"openstack_image_ids", "openstack_image_names"}:
            continue
        require_nonempty_string(entry, name)

    if target == "aws":
        for name in AWS_REQUIRED:
            require_nonempty_string(entry, name)
        if entry.get("aws_account_id") is not None:
            require_nonempty_string(entry, "aws_account_id")

    if target == "openstack":
        ids = validate_string_list(entry, "openstack_image_ids")
        names = validate_string_list(entry, "openstack_image_names")
        require_nonempty_string(entry, "openstack_project")
        project_id = entry.get("openstack_project_id")
        if project_id is not None:
            require_nonempty_string(entry, "openstack_project_id")
        region = entry.get("openstack_region")
        if region is not None and (not isinstance(region, str) or not region.strip()):
            raise ValueError(
                "openstack_region must be a non-empty string when present")
        if len(ids) != len(disks):
            raise ValueError(
                "openstack_image_ids count must match artifact disk count"
            )
        if len(names) != len(ids):
            raise ValueError(
                "openstack_image_names must match openstack_image_ids"
            )
        validate_image_order(names, disks, "openstack_image_names entry")

    if target == "gcp":
        for name in GCP_REQUIRED:
            require_nonempty_string(entry, name)
        data_names = validate_string_list(
            entry, "gcp_data_image_names", allow_empty=True
        )
        data_links = validate_string_list(
            entry, "gcp_data_image_selflinks", allow_empty=True
        )
        if len(data_links) != len(data_names):
            raise ValueError(
                "gcp_data_image_selflinks must match gcp_data_image_names"
            )
        if len(data_names) != len(disks) - 1:
            raise ValueError(
                "GCP data image count must equal artifact disk count minus one"
            )
        validate_image_order(
            [entry["gcp_boot_image_name"], *data_names], disks,
            "GCP image name")
        if not all(
            urlparse(value).scheme in {"http", "https"}
            for value in [entry["gcp_boot_image_selflink"], *data_links]
        ):
            raise ValueError(
                "GCP image selflinks must be absolute HTTP(S) URLs")

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
        catalog = load_or_init_catalog(path, "migrations")

        # Unlike the DIB7 image catalog, this one accumulates rather than
        # supersedes: an import_task_id always mints a genuinely new,
        # independently usable AMI (AWS never deregisters an old one behind
        # our back), so an older record for the same vm_name is still valid
        # and worth keeping. The only thing keyed for replacement is an exact
        # rerun against the same import_task_id, which happens if this script
        # is invoked twice for one migration (e.g. a retried playbook step).
        # AWS reruns are identified by their import task. OpenStack and GCP
        # imports have no EC2 task ID, so their unique snapshot-backed clone
        # name is the run identity instead.
        entry_identity = migration_identity(entry)
        catalog["migrations"] = [
            old for old in catalog["migrations"]
            if migration_identity(old) != entry_identity
        ]
        catalog["migrations"].append(entry)
        write_catalog(path, catalog, "migrations")

if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"append-migration-catalog: {error}")
