#!/usr/bin/env python3
# bin/lookup-migration-openstack.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Find the newest completed OpenStack migration for a VM."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from lookup_migration_common import main  # noqa: E402

def project(entry):
    return {
        "vm_name": entry["vm_name"],
        "openstack_image_ids": entry["openstack_image_ids"],
        "openstack_image_names": entry.get("openstack_image_names", []),
        "openstack_project": entry["openstack_project"],
        "openstack_project_id": entry.get("openstack_project_id", ""),
        "artifact_manifest": entry["artifact_manifest"],
        "migration_id": entry.get("clone_name", ""),
    }

def project_filter(args):
    """Restrict to one project, and to one region when the caller has one.

    Project matches either its name or its ID. A region filter skips rows
    recorded for a different region. Rows written before openstack_region
    existed stay eligible, matching how reconcile treats those legacy rows.
    """
    wanted_project = None if args.project is None else str(args.project).strip()
    wanted_region = None if not getattr(args, "region", None) else str(args.region).strip()
    if wanted_project is None and not wanted_region:
        return None

    def accept(entry):
        if wanted_project is not None and wanted_project not in {
            str(entry.get("openstack_project", "")),
            str(entry.get("openstack_project_id", "")),
        }:
            return False
        recorded = entry.get("openstack_region")
        if wanted_region and isinstance(recorded, str) and recorded.strip():
            return recorded == wanted_region
        return True

    return accept

if __name__ == "__main__":
    main(
        "lookup-migration-openstack",
        "openstack",
        project,
        parser_options={"with_project": True, "with_region": True},
        entry_filter=project_filter,
    )
