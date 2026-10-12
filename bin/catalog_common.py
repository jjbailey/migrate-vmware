#!/usr/bin/env python3
# bin/catalog_common.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Shared catalog locking, validation, and atomic read/write helpers.

Every script that touches catalogs/*.json goes through this module: the
append scripts, the reconcile scripts, and the lookups. The durability
path (take the lock, write a temporary file, fsync, rename over the
original) is subtle enough that a second copy of it would eventually
drift from this one.
"""

import contextlib
import datetime as dt
import fcntl
import json
import os
import stat
import tempfile

def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def parse_timestamp(value):
    """Timezone-aware datetime for an ISO-8601 string, or None if unusable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None

def timestamp_sort_key(value):
    """Chronological sort key for an ISO-8601 string; unparseable sorts first.

    Offsets other than Z are legal, so comparing the raw strings can put an
    older row ahead of a newer one.
    """
    return parse_timestamp(value) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)

def load_catalog(path, list_key):
    if not path.exists():
        raise ValueError(f"no catalog at {path}")
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1 or not isinstance(catalog.get(list_key), list):
        raise ValueError(
            f"{path}: expected schema_version 1 and a {list_key} list")
    return catalog

def load_or_init_catalog(path, list_key):
    """Like load_catalog, but an absent catalog starts out empty."""
    if not path.exists():
        return {"schema_version": 1, list_key: []}
    return load_catalog(path, list_key)

def atomic_write_json(path, payload):
    """Replace path with payload, keeping its mode and surviving a crash."""
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            mode = 0o644
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def write_catalog(path, catalog, list_key):
    timestamp = "migrated_at" if list_key == "migrations" else "launched_at"
    catalog[list_key].sort(key=lambda item: (
        item.get("vm_name", ""), timestamp_sort_key(item.get(timestamp))))
    catalog["generated_at"] = now_utc()
    atomic_write_json(path, catalog)

@contextlib.contextmanager
def exclusive_lock(path):
    """Hold the catalog's sidecar lock for the duration of the block."""
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

def require_nonempty_string(entry, name):
    value = entry.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be a non-empty string")

def validate_timestamp(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be a non-empty ISO-8601 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(name + " must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(name + " must include a timezone")

def validate_string_list(entry, name, allow_empty=False):
    values = entry.get(name)
    if not isinstance(values, list) or (not allow_empty and not values):
        adjective = "list" if allow_empty else "non-empty list"
        raise ValueError(name + " must be a " + adjective + " of strings")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError(name + " must contain only non-empty strings")
    return values

def require_missing_fields(entry, required, context=None):
    """Raise if any required key is absent, naming every one that is."""
    missing = sorted(required - entry.keys())
    if missing:
        where = " for " + context if context else ""
        raise ValueError(
            "catalog entry missing" + where + ": " + ", ".join(missing))

def read_snapshot(path, list_key):
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        catalog = load_catalog(path, list_key)
        fcntl.flock(lock, fcntl.LOCK_UN)
    return catalog

def _row_fingerprint(entry):
    """Stable identity of one catalog row, not of its cloud resource.

    Reconcile discovers specific rows, then re-reads the catalog under the
    lock. Matching only clone_name / server ID / import task would also delete
    a different VM, region, or account that happens to reuse that ID, and a
    missing region or account field used to count as "in scope". The
    fingerprint is the whole row, so only the row that was discovered goes.
    """
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))

def _requested_fingerprints(stale, retired):
    fingerprints = {_row_fingerprint(entry) for entry, *_ in stale}
    fingerprints |= {_row_fingerprint(entry) for entry in retired}
    return fingerprints

def _remove(path, list_key, fingerprints):
    """Delete the exact rows reconcile discovered, and nothing else."""
    if not fingerprints:
        return 0
    changed = 0
    with exclusive_lock(path):
        catalog = load_catalog(path, list_key)
        retained = []
        for entry in catalog[list_key]:
            if _row_fingerprint(entry) in fingerprints:
                changed += 1
            else:
                retained.append(entry)
        catalog[list_key] = retained
        if changed:
            write_catalog(path, catalog, list_key)
    return changed

def remove_migration_entries(path, stale, retired, target_platform, identity_field, region=None, project_id=None):
    # target_platform, identity_field, region, and project_id are retained so
    # existing reconcilers keep a stable call shape. Removal itself is by the
    # exact row objects those reconcilers already scoped.
    del target_platform, identity_field, region, project_id
    return _remove(path, "migrations", _requested_fingerprints(stale, retired))

def remove_deployment_entries(path, stale, retired, target_platform, identity_field, region=None, project_id=None):
    del target_platform, identity_field, region, project_id
    return _remove(path, "deployments", _requested_fingerprints(stale, retired))
