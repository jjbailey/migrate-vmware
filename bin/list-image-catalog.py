#!/usr/bin/env python3
# bin/list-image-catalog.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Print a filterable report of migrated images without querying the clouds."""

import argparse
import json
import shutil
import textwrap
from pathlib import Path

from catalog_common import read_snapshot, timestamp_sort_key

DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / "catalogs" / "migration-catalog.json"

def project_values(entry):
    platform = entry.get("target_platform")
    fields = {
        "aws": ("aws_project", "aws_account_id"),
        "openstack": ("openstack_project", "openstack_project_id"),
        "gcp": ("gcp_project",),
    }.get(platform, ())
    return [str(entry[field]) for field in fields if entry.get(field)]

def region_value(entry):
    field = {"aws": "aws_region", "openstack": "openstack_region",
             "gcp": "gcp_import_location"}.get(entry.get("target_platform"))
    return entry.get(field, "") if field else ""

def matches(entry, args):
    return (
        (args.status == "all" or entry.get("status") == args.status)
        and (not args.provider or entry.get("target_platform") == args.provider)
        and (not args.vm_name or entry.get("vm_name") == args.vm_name)
        and (not args.project or args.project in project_values(entry))
        and (not args.region or region_value(entry) == args.region)
    )

def print_field(label, value, width):
    prefix = f"  {label:<16} "
    text = "-" if value is None or value == "" else str(value)
    lines = textwrap.wrap(text, width=max(20, width - len(prefix)),
                          break_long_words=True, break_on_hyphens=False) or [""]
    print(prefix + lines[0])
    for line in lines[1:]:
        print(" " * len(prefix) + line)

def print_images(entry, width):
    platform = entry.get("target_platform")
    if platform == "aws":
        print_field("AMI", entry.get("ami_id"), width)
    elif platform == "openstack":
        ids = entry.get("openstack_image_ids") or []
        names = entry.get("openstack_image_names") or []
        for index in range(max(len(ids), len(names))):
            print_field(f"Disk {index} ID", ids[index] if index < len(ids) else None, width)
            print_field(f"Disk {index} name", names[index] if index < len(names) else None, width)
    elif platform == "gcp":
        print_field("Boot image", entry.get("gcp_boot_image_name"), width)
        print_field("Boot image URL", entry.get("gcp_boot_image_selflink"), width)
        names = entry.get("gcp_data_image_names") or []
        links = entry.get("gcp_data_image_selflinks") or []
        for index in range(max(len(names), len(links))):
            print_field(f"Disk {index + 1} name", names[index] if index < len(names) else None, width)
            print_field(f"Disk {index + 1} URL", links[index] if index < len(links) else None, width)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG,
                        help="catalog JSON file (default: repository's catalogs/migration-catalog.json)")
    parser.add_argument("--provider", "--platform", dest="provider",
                        choices=("aws", "gcp", "openstack"), help="filter by target platform")
    parser.add_argument("--vm-name", help="filter by exact source VM name")
    parser.add_argument("--project", help="filter by project name/key/ID or AWS account ID")
    parser.add_argument("--region", help="filter by exact region (GCP: import location); excludes unrecorded regions")
    parser.add_argument("--status", default="completed", help="status to show, or all (default: completed)")
    parser.add_argument("--json", action="store_true", help="print matching migration rows as a JSON list")
    args = parser.parse_args(argv)

    try:
        catalog = read_snapshot(args.catalog, "migrations")
        entries = catalog["migrations"]
        if any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("every catalog migration must be an object")
        for entry in entries:
            for field in ("openstack_image_ids", "openstack_image_names", "gcp_data_image_names",
                          "gcp_data_image_selflinks"):
                value = entry.get(field)
                if value is not None and (not isinstance(value, list)
                                          or any(not isinstance(item, str) for item in value)):
                    raise ValueError(f"{field} must be a list of strings")
    except AttributeError as error:
        raise SystemExit("list-image-catalog: expected a catalog object with schema_version 1 and a migrations list") from error
    except (OSError, ValueError) as error:
        raise SystemExit(f"list-image-catalog: {error}") from error

    selected = [entry for entry in entries if matches(entry, args)]
    selected.sort(key=lambda entry: timestamp_sort_key(entry.get("migrated_at")), reverse=True)
    selected.sort(key=lambda entry: (str(entry.get("vm_name", "")),
                                     str(entry.get("target_platform", ""))))
    if args.json:
        print(json.dumps(selected, indent=2))
        return 0

    print(f"Image catalog: {args.catalog}")
    print(f"Entries: {len(selected)} ({args.status})")
    filters = [f"{name}={getattr(args, name)}" for name in ("provider", "vm_name", "project", "region")
               if getattr(args, name)]
    if filters:
        print("Filters: " + "; ".join(filters))
    if not selected:
        print("No matching catalog entries.")
        return 0

    width = max(72, shutil.get_terminal_size(fallback=(100, 20)).columns)
    for index, entry in enumerate(selected):
        if index:
            print()
        print(f"{entry.get('vm_name', '<unnamed>')} [{entry.get('target_platform', '?')}]")
        for label, value in (
            ("Status", entry.get("status")),
            ("Migrated at", entry.get("migrated_at")),
            ("Migration ID", entry.get("clone_name")),
            ("Source vCenter", entry.get("source_vcenter")),
            ("Datacenter", entry.get("source_datacenter")),
            ("Project/account", ", ".join(project_values(entry))),
            ("Region/location", region_value(entry)),
            ("Artifact type", entry.get("artifact_type")),
        ):
            print_field(label, value, width)
        print_images(entry, width)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())