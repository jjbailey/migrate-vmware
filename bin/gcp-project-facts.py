#!/usr/bin/env python3
# bin/gcp-project-facts.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""List the Compute zones, machine types, networks, and subnetworks one
GCP project can use.

Credentials come from vaults/gcp.yml (gcp_projects, matched by vault key,
gcp_project, or project_id, the same way select-project.yml does). The
service_account_key JSON in the vault entry authenticates the Compute API.

Machine types are zonal, so --machine-types needs --zone. Subnetworks are
regional, so --subnetworks needs --region (or --zone, from which the region
is derived). Zones and networks are global to the project.

Output is a table by default, or JSON with --json for scripting
(launch-gcp-dialog.sh uses this for its zone, machine-type, network, and
subnetwork pickers).
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from vault_common import read_vault  # noqa: E402

COMPUTE_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
COMPUTE_API = "https://compute.googleapis.com/compute/v1/projects"

def select_vault_entry(vault, requested):
    data = read_vault(vault)
    projects = {str(key): value for key, value in (
        data.get("gcp_projects") or {}).items()}
    if requested:
        for key, entry in projects.items():
            entry = entry or {}
            if requested in {key, str(entry.get("gcp_project", "")),
                             str(entry.get("project_id", ""))}:
                return key, entry
        if not projects and data.get("gcp_project"):
            pid = str(data.get("gcp_project", ""))
            if requested in {pid, "legacy"}:
                return "legacy", data
        raise SystemExit(
            f"GCP project '{requested}' matches no vault key, gcp_project, "
            f"or project_id in {vault}. "
            f"Available: {', '.join(projects) or '<none>'}"
        )
    if not projects and data.get("gcp_project"):
        return "legacy", data
    if len(projects) == 1:
        return next(iter(projects.items()))
    raise SystemExit(
        f"{vault} holds several projects; name one as the PROJECT argument "
        f"({', '.join(projects) or '<none>'})"
    )

def make_session(entry):
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession

    raw = entry.get("service_account_key", "")
    try:
        info = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"service_account_key is not valid JSON: {error}")
    if not info.get("client_email"):
        raise SystemExit("service_account_key has no client_email")
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=[COMPUTE_SCOPE])
    return AuthorizedSession(creds)

def api_list(session, url, params=None):
    items = []
    page_token = None
    while True:
        query = dict(params or {})
        if page_token:
            query["pageToken"] = page_token
        response = session.get(url, params=query or None)
        response.raise_for_status()
        body = response.json()
        items.extend(body.get("items", []) or [])
        page_token = body.get("nextPageToken")
        if not page_token:
            return items

def short_name(url):
    return str(url or "").rstrip("/").split("/")[-1]

def region_from_zone(zone):
    return re.sub(r"-[a-z]$", "", str(zone or "").strip())

def gather_zones(session, project):
    zones = []
    for zone in api_list(session, f"{COMPUTE_API}/{project}/zones"):
        if str(zone.get("status", "")).upper() != "UP":
            continue
        zones.append({"name": zone.get("name", ""),
                      "region": region_from_zone(zone.get("name", "")),
                      "status": zone.get("status", "")})
    zones.sort(key=lambda item: item["name"])
    return zones

def gather_machine_types(session, project, zone):
    seen = set()
    types = []
    for machine in api_list(
            session,
            f"{COMPUTE_API}/{project}/zones/{zone}/machineTypes"):
        name = machine.get("name", "")
        if not name or name in seen:
            continue
        if (machine.get("deprecated") or {}).get("state"):
            continue
        seen.add(name)
        types.append({"name": name,
                      "vcpus": machine.get("guestCpus", 0),
                      "memory_mb": machine.get("memoryMb", 0)})
    types.sort(key=lambda item: item["name"])
    return types

def gather_networks(session, project):
    nets = []
    for net in api_list(session, f"{COMPUTE_API}/{project}/global/networks"):
        auto = net.get("autoCreateSubnetworks")
        nets.append({"name": net.get("name", ""),
                     "mode": "auto" if auto else "custom",
                     "routing": net.get("routingConfig", {}).get(
                         "routingMode", "")})
    nets.sort(key=lambda item: (item["name"] != "default", item["name"]))
    return nets

def gather_subnetworks(session, project, region):
    subs = []
    for sub in api_list(
            session,
            f"{COMPUTE_API}/{project}/regions/{region}/subnetworks"):
        subs.append({"name": sub.get("name", ""),
                     "cidr": sub.get("ipCidrRange", ""),
                     "network": short_name(sub.get("network", "")),
                     "state": sub.get("state", "")})
    subs.sort(key=lambda item: item["name"])
    return subs

def print_table(title, rows, columns):
    print(f"{title}: {len(rows)}")
    if not rows:
        return
    widths = {column: len(column) for column in columns}
    for row in rows:
        for column in columns:
            widths[column] = max(widths[column],
                                 len(str(row.get(column, ""))))
    header = "  " + "  ".join(
        column.upper().ljust(widths[column]) for column in columns)
    print(header)
    for row in rows:
        print("  " + "  ".join(
            str(row.get(column, "")).ljust(widths[column])
            for column in columns))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", nargs="?",
                        help="vault key, gcp_project, or project_id "
                             "(omit for a single-entry vault)")
    parser.add_argument("--vault", default=str(REPO_DIR / "vaults/gcp.yml"))
    parser.add_argument("--zones", dest="list_zones", action="store_true",
                        help="list up zones")
    parser.add_argument("--machine-types", dest="list_types",
                        action="store_true",
                        help="list machine types in --zone")
    parser.add_argument("--networks", dest="list_networks",
                        action="store_true",
                        help="list networks")
    parser.add_argument("--subnetworks", dest="list_subnets",
                        action="store_true",
                        help="list subnetworks in --region (or --zone)")
    parser.add_argument("--zone", help="zone for --machine-types")
    parser.add_argument("--region", help="region for --subnetworks")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="machine-readable output for scripting")
    args = parser.parse_args()

    key, entry = select_vault_entry(args.vault, args.project)
    entry = entry or {}
    project_id = str(entry.get("gcp_project", "") or "").strip()
    if not project_id:
        raise SystemExit(f"vault entry '{key}' has no gcp_project")

    try:
        import google.auth.exceptions  # noqa: F401
    except ImportError:
        raise SystemExit("google-auth is required (see README)")

    modes = [name for name, wanted in (
        ("zones", args.list_zones), ("machine_types", args.list_types),
        ("networks", args.list_networks),
        ("subnetworks", args.list_subnets)) if wanted]
    if len(modes) > 1:
        raise SystemExit("pick one of --zones/--machine-types/"
                         "--networks/--subnetworks")
    mode = modes[0] if modes else "zones"

    zone = str(args.zone or "").strip()
    region = str(args.region or "").strip() or (
        region_from_zone(zone) if zone else "")

    session = make_session(entry)
    try:
        if mode == "zones":
            rows = gather_zones(session, project_id)
        elif mode == "machine_types":
            if not zone:
                raise SystemExit("--machine-types needs --zone")
            rows = gather_machine_types(session, project_id, zone)
        elif mode == "networks":
            rows = gather_networks(session, project_id)
        else:
            if not region:
                raise SystemExit("--subnetworks needs --region (or --zone)")
            rows = gather_subnetworks(session, project_id, region)
    except SystemExit:
        raise
    except Exception as error:
        raise SystemExit(f"could not list {mode}: {error}")

    if args.as_json:
        json.dump({"project": key, "gcp_project": project_id,
                   "zone": zone, "region": region, mode: rows},
                  sys.stdout, indent=2, default=str)
        print()
        return 0

    columns = {"zones": ["name", "region", "status"],
               "machine_types": ["name", "vcpus", "memory_mb"],
               "networks": ["name", "mode", "routing"],
               "subnetworks": ["name", "cidr", "network",
                               "state"]}[mode]
    print(f"GCP project: {project_id}")
    print_table(mode.replace("_", " ").capitalize(), rows, columns)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(f"gcp-project-facts: {error}")
