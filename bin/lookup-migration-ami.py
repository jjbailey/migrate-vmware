#!/usr/bin/env python3
# bin/lookup-migration-ami.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Find the most recent completed AWS migration for a VM and print its AMI."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from lookup_migration_common import main  # noqa: E402

def project(entry):
    return {
        "ami_id": entry["ami_id"],
        "import_task_id": entry["import_task_id"],
        "migrated_at": entry["migrated_at"],
        "aws_region": entry["aws_region"],
        "aws_account_id": entry.get("aws_account_id", ""),
    }

def account_filter(args, legacy=False):
    """Rows recorded for --project's account, or with legacy, rows with none.

    Rows written before aws_account_id existed could be from any account, so
    they are only a fallback when no row names the account (see main below).
    """
    if not args.project:
        return None
    wanted = str(args.project)
    if legacy:
        return lambda entry: not entry.get("aws_account_id")
    return lambda entry: (str(entry.get("aws_account_id") or "") == wanted
                          or entry.get("aws_project") == wanted)

def region_filter(args, legacy=False):
    account = account_filter(args, legacy)
    if not args.region:
        return account
    wanted = args.region.strip()
    return lambda entry: (entry.get("aws_region") == wanted
                          and (account is None or account(entry)))

def legacy_region_filter(args):
    if not args.project:
        return None
    return region_filter(args, legacy=True)

if __name__ == "__main__":
    main(
        "lookup-migration-ami",
        "aws",
        project,
        parser_options={"with_status": True, "with_project": True, "with_region": True},
        entry_filter=region_filter,
        fallback_filter=legacy_region_filter,
    )
