#!/usr/bin/env python3
# bin/lookup_migration_common.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Shared lookup of the newest usable migration row for one VM.

The three lookup-migration-* entry points differ only in which platform they
filter on and which fields the launch playbooks need back, so that is all each
one supplies.
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from catalog_common import parse_timestamp, read_snapshot, timestamp_sort_key  # noqa: E402

def latest_migration(catalog_path, vm_name, platform, status="completed",
                     entry_filter=None, tool="lookup-migration"):
    if not catalog_path.exists():
        raise SystemExit(f"{tool}: no catalog at {catalog_path}")
    # Same sidecar lock as append and reconcile, so a launch does not read a
    # catalog mid-replace.
    catalog = read_snapshot(catalog_path, "migrations")
    matches = [
        entry for entry in catalog.get("migrations", [])
        if entry.get("vm_name") == vm_name
        and entry.get("target_platform") == platform
        and entry.get("status") == status
        # A row without a usable timestamp cannot be ordered against the
        # others, so it is not a candidate for "newest".
        and parse_timestamp(entry.get("migrated_at")) is not None
        and (entry_filter is None or entry_filter(entry))
    ]
    if not matches:
        return None
    # Compare parsed instants: migrated_at may carry any UTC offset.
    return max(matches, key=lambda entry: timestamp_sort_key(entry["migrated_at"]))

def build_parser(with_status=False, with_project=False, with_region=False):
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True, type=pathlib.Path)
    parser.add_argument("--vm-name", required=True)
    if with_status:
        parser.add_argument("--status", default="completed")
    if with_project:
        parser.add_argument(
            "--project",
            help="only consider migrations for this platform project (name or ID)",
        )
    if with_region:
        parser.add_argument(
            "--region",
            help="only consider migrations in this region; legacy rows with no region stay eligible",
        )
    return parser

def run(tool, platform, project, parser_options=None, entry_filter=None,
        fallback_filter=None):
    """Parse arguments, find the newest row, and print the projected fields.

    project is a callable turning one catalog row into the JSON object the
    caller's playbook consumes. fallback_filter, when it returns a filter, is
    tried only if entry_filter matches nothing.
    """
    args = build_parser(**(parser_options or {})).parse_args()
    filters = [entry_filter]
    if fallback_filter is not None and fallback_filter(args) is not None:
        filters.append(fallback_filter)
    latest = None
    for make_filter in filters:
        latest = latest_migration(
            args.catalog,
            args.vm_name,
            platform,
            status=getattr(args, "status", "completed"),
            entry_filter=None if make_filter is None else make_filter(args),
            tool=tool,
        )
        if latest is not None:
            break
    if latest is None:
        raise SystemExit(
            f"{tool}: no usable {getattr(args, 'status', 'completed')!r} "
            f"{platform} migration found for vm_name={args.vm_name!r} "
            f"in {args.catalog}"
        )
    print(json.dumps(project(latest)))

def main(tool, platform, project, parser_options=None, entry_filter=None,
         fallback_filter=None):
    """Entry point wrapper turning expected failures into a clean exit."""
    try:
        run(tool, platform, project, parser_options, entry_filter,
            fallback_filter)
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as error:
        raise SystemExit(f"{tool}: {error}")
