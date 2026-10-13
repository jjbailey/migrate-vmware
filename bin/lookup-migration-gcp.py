#!/usr/bin/env python3
# bin/lookup-migration-gcp.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Find the newest completed GCP migration for a VM."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from lookup_migration_common import main  # noqa: E402

def project_filter(args):
    if args.project is None:
        return None
    wanted = str(args.project).strip()
    return lambda entry: str(entry.get("gcp_project", "")) == wanted

def project(entry):
    return {
        "vm_name": entry["vm_name"],
        "gcp_project": entry["gcp_project"],
        "gcp_boot_image_name": entry["gcp_boot_image_name"],
        "gcp_boot_image_selflink": entry["gcp_boot_image_selflink"],
        "gcp_data_image_names": entry.get("gcp_data_image_names", []),
        "gcp_data_image_selflinks": entry.get("gcp_data_image_selflinks", []),
        "migration_id": entry.get("clone_name", ""),
    }

if __name__ == "__main__":
    main(
        "lookup-migration-gcp",
        "gcp",
        project,
        parser_options={"with_project": True},
        entry_filter=project_filter,
    )
