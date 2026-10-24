#!/usr/bin/env python3
# bin/openstack-project-facts.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Show the images, security groups, networks, and keypairs one OpenStack
project can use.

Credentials come from vaults/openstack.yml (openstack_projects, matched by
vault key, project_name, or project_id, the same way select-project.yml does),
or from --cloud / the standard OS_* environment. Every listing is filtered to
what the authenticated project can actually use, so an admin account does not
see other projects' resources:

  images           active, and public, owned by the project, or shared with
                   it (membership accepted)
  security groups  owned by the project, or RBAC-shared to it
  networks         owned by the project, shared, RBAC-shared to it, or
                   external and open to it (external ones are marked; they
                   serve floating IPs and only take ports directly when also
                   shared)
  keypairs         Nova keypairs belong to the user, not the project, so this
                   is the authenticated user's keypairs
  quotas           compute, volume, and network usage as used/total, flagged
                   LOW at --warn-percent and FULL at the limit; --quotas
                   shows only this section
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import keystoneauth1.exceptions
import openstack
import yaml

REPO_DIR = Path(__file__).resolve().parents[1]

def ansible_vault_bin():
    venv_bin = Path(os.environ.get("DIB7_VENV_BIN", Path.home() / ".dib7/bin"))
    candidate = venv_bin / "ansible-vault"
    if os.access(candidate, os.X_OK):
        return str(candidate)
    found = shutil.which("ansible-vault")
    if not found:
        raise SystemExit(
            "ansible-vault not found in $DIB7_VENV_BIN or on PATH")
    return found

def read_vault(vault):
    """Decrypt the vault. Password lookup matches edit-vault.sh: explicit
    file, default file, then ansible-vault's own prompt."""
    command = [ansible_vault_bin(), "view", str(vault)]
    for passfile in (os.environ.get("VAULT_PASSWORD_FILE"),
                     str(Path.home() / ".ssh/dib-vault-pass")):
        if passfile and Path(passfile).is_file() and Path(passfile).stat().st_size:
            command += ["--vault-password-file", passfile]
            break
    result = subprocess.run(
        command, stdout=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"could not decrypt {vault}")
    return yaml.safe_load(result.stdout) or {}

def select_vault_auth(vault, requested):
    data = read_vault(vault)
    projects = {str(key): value for key, value in (
        data.get("openstack_projects") or {}).items()}
    if requested:
        for key, auth in projects.items():
            if requested in {key, str(auth.get("project_name", "")), str(auth.get("project_id", ""))}:
                return key, auth
        raise SystemExit(
            f"OpenStack project '{requested}' matches no vault key, project_name, "
            f"or project_id in {vault}. Available: {', '.join(projects) or '<none>'}"
        )
    if data.get("openstack_auth"):
        auth = data["openstack_auth"]
        return str(auth.get("project_name", "")), auth
    if len(projects) == 1:
        return next(iter(projects.items()))
    raise SystemExit(
        f"{vault} holds several projects; name one as the PROJECT argument "
        f"({', '.join(projects) or '<none>'})"
    )

def connect(args):
    if args.cloud:
        if args.project:
            print(f"warning: ignoring PROJECT '{args.project}'; --cloud "
                  f"'{args.cloud}' picks the project", file=sys.stderr)
        return openstack.connect(cloud=args.cloud, region_name=args.region_name)
    env_auth = os.environ.get("OS_CLOUD") or os.environ.get("OS_AUTH_URL")
    if env_auth and not args.project:
        return openstack.connect(region_name=args.region_name)
    _, auth = select_vault_auth(args.vault, args.project)
    options = {key: (str(value) if key == "project_id" else value)
               for key, value in auth.items() if value not in (None, "")}
    if args.region_name:
        options["region_name"] = args.region_name
    # Only the vault entry: an OS_* openrc or clouds.yaml for another project
    # must not fill in what the entry leaves out.
    return openstack.connect(load_envvars=False, load_yaml_config=False,
                             **options)

def rbac_policies(conn, object_type, action):
    """RBAC policies the user can list, or None when listing is refused."""
    try:
        return list(conn.network.rbac_policies(
            object_type=object_type, action=action))
    except openstack.exceptions.SDKException:
        return None

def rbac_shared_ids(conn, project_id, object_type):
    """Object IDs shared to this project (or to everyone) by RBAC policy.

    Non-admin users may not be allowed to list policies; Neutron already
    reports RBAC-shared networks and security groups as shared for them
    (is_shared), so failure is harmless.
    """
    policies = rbac_policies(conn, object_type, "access_as_shared") or []
    return {p.object_id for p in policies if p.target_project_id in (project_id, "*")}

def external_access(conn, project_id):
    """Return a test for whether this project may use an external network.

    A non-admin user only sees external networks open to their project, so
    every one listed counts. An admin sees them all, so when the
    access_as_external policies can be listed, a network that has any must
    have one for this project or '*'. One with none visible is kept unless
    the listing shows other projects' policies, which means the caller sees
    everything (an admin) and the network is open to no one else.
    """
    policies = rbac_policies(conn, "network", "access_as_external")
    if policies is None:
        return lambda network: True
    targets = {}
    for p in policies:
        targets.setdefault(p.object_id, set()).add(p.target_project_id)
    sees_all = any(p.project_id != project_id for p in policies)

    def accept(network):
        if network.id in targets:
            return bool(targets[network.id] & {project_id, "*"})
        return not sees_all
    return accept

def image_member_accepted(conn, image, project_id):
    try:
        member = conn.image.get_member(project_id, image)
    except openstack.exceptions.SDKException:
        return False
    return member.status == "accepted"

def gather_images(conn, project_id):
    images = []
    for image in conn.image.images():
        if image.status != "active":
            continue
        if image.owner_id == project_id:
            access = "owned"
        elif image.visibility == "public":
            access = "public"
        elif image.visibility == "shared" and image_member_accepted(conn, image, project_id):
            access = "shared"
        else:
            continue
        images.append({
            "name": image.name,
            "id": image.id,
            "access": access,
            "visibility": image.visibility,
            "disk_format": image.disk_format,
            "size_gb": round((image.size or 0) / 1024 ** 3, 1),
            "created": image.created_at,
        })
    return sorted(images, key=lambda i: (i["name"] or "", i["created"] or ""))

def gather_security_groups(conn, project_id):
    shared = rbac_shared_ids(conn, project_id, "security_group")
    groups = []
    for group in conn.network.security_groups():
        if group.project_id == project_id:
            access = "owned"
        elif group.id in shared or group.is_shared:
            access = "rbac-shared"
        else:
            continue
        groups.append({
            "name": group.name,
            "id": group.id,
            "access": access,
            "rules": len(group.security_group_rules or []),
            "description": group.description or "",
        })
    return sorted(groups, key=lambda g: g["name"] or "")

def gather_networks(conn, project_id):
    shared = rbac_shared_ids(conn, project_id, "network")
    external_ok = external_access(conn, project_id)
    cidrs = {}
    for subnet in conn.network.subnets():
        cidrs.setdefault(subnet.network_id, []).append(subnet.cidr)
    networks = []
    for network in conn.network.networks():
        if network.project_id == project_id:
            access = "owned"
        elif network.is_shared or network.id in shared:
            access = "shared"
        elif network.is_router_external and external_ok(network):
            access = "external"
        else:
            continue
        networks.append({
            "name": network.name,
            "id": network.id,
            "access": access,
            "external": bool(network.is_router_external),
            "status": network.status,
            "subnets": sorted(cidrs.get(network.id, [])),
        })
    return sorted(networks, key=lambda n: n["name"] or "")

def gather_keypairs(conn):
    return sorted(
        ({"name": key.name, "type": key.type, "fingerprint": key.fingerprint}
         for key in conn.compute.keypairs()),
        key=lambda k: k["name"],
    )

# Nova's key_pairs quota is per user and the quota set reports no usage for
# it, so it is left out rather than shown as a misleading 0/N.
COMPUTE_QUOTAS = (("instances", ""), ("cores", ""),
                  ("ram", "MB"), ("server_groups", ""))
VOLUME_QUOTAS = (("volumes", ""), ("gigabytes", "GB"),
                 ("snapshots", ""), ("backups", ""), ("backup_gigabytes", "GB"))
NETWORK_QUOTAS = ("floating_ips", "networks", "subnets", "ports",
                  "routers", "security_groups", "security_group_rules")

def quota_row(service, resource, used, limit, unit, warn_percent):
    used = used or 0
    if limit is None or limit < 0:
        free, percent, status = None, None, ""
    elif limit == 0:
        # Clouds may still pre-create some (Neutron's default security
        # group), but no more can be added.
        free, percent, status = 0, None, "none allowed"
    else:
        free = max(limit - used, 0)
        percent = round(100 * used / limit)
        status = "FULL" if used >= limit else "LOW" if percent >= warn_percent else ""
    total = "unlimited" if limit is None or limit < 0 else limit
    return {
        "service": service,
        "resource": resource + (f" ({unit})" if unit else ""),
        "used": used,
        "limit": None if total == "unlimited" else limit,
        "used_total": f"{used}/{total}",
        "free": "" if free is None else free,
        "percent": "" if percent is None else f"{percent}%",
        "status": status,
    }

def gather_quotas(conn, project_id, warn_percent):
    """Used/limit for the resources a deployment consumes.

    Cinder's per-volume-type quotas are shown only when they set a real
    limit; unlimited ones are noise next to the overall volume totals.
    """
    rows = []
    try:
        quota = conn.compute.get_quota_set(project_id, usage=True)
        for resource, unit in COMPUTE_QUOTAS:
            rows.append(quota_row("compute", resource, quota.usage.get(resource),
                                  getattr(quota, resource), unit, warn_percent))
    except openstack.exceptions.SDKException as exc:
        print(f"warning: compute quotas unavailable: {exc}", file=sys.stderr)
    try:
        quota = conn.block_storage.get_quota_set(project_id, usage=True)
        limits = quota.to_dict(computed=False)
        resources = list(VOLUME_QUOTAS) + [
            (key, "GB" if key.startswith("gigabytes_") else "")
            for key in sorted(quota.usage)
            if "_" in key and key.split("_", 1)[0] in ("volumes", "gigabytes", "snapshots")
            and (limits.get(key) if limits.get(key) is not None else -1) >= 0
        ]
        for resource, unit in resources:
            rows.append(quota_row("volume", resource, quota.usage.get(resource),
                                  limits.get(resource), unit, warn_percent))
    except openstack.exceptions.SDKException as exc:
        print(f"warning: volume quotas unavailable: {exc}", file=sys.stderr)
    try:
        quota = conn.network.get_quota(project_id, details=True)
        for resource in NETWORK_QUOTAS:
            detail = getattr(quota, resource) or {}
            rows.append(quota_row("network", resource, detail.get("used"),
                                  detail.get("limit"), "", warn_percent))
    except openstack.exceptions.SDKException as exc:
        print(f"warning: network quotas unavailable: {exc}", file=sys.stderr)
    return rows

def print_table(title, rows, columns):
    print(f"\n{title} ({len(rows)})")
    if not rows:
        print("  <none>")
        return
    cells = [[", ".join(v) if isinstance(v := row[c], list) else str(v if v is not None else "")
              for c in columns] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in cells))
              for i, c in enumerate(columns)]
    print("  " + "  ".join(c.upper().ljust(w)
          for c, w in zip(columns, widths)).rstrip())
    for row in cells:
        print("  " + "  ".join(v.ljust(w)
              for v, w in zip(row, widths)).rstrip())

def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "project", nargs="?", metavar="PROJECT",
        help="vault key, project_name, or project_id in openstack_projects; "
        "optional when the vault has one project or OS_* auth is set")
    parser.add_argument("--vault", type=Path,
                        default=REPO_DIR / "vaults/openstack.yml")
    parser.add_argument(
        "--cloud", help="named clouds.yaml entry instead of the vault")
    parser.add_argument(
        "--region-name", help="override the vault's region_name")
    parser.add_argument("--json", action="store_true",
                        help="print JSON instead of tables")
    parser.add_argument("--quotas", action="store_true",
                        help="show only quota usage")
    parser.add_argument(
        "--warn-percent", type=int, default=80, metavar="N",
        help="flag a quota LOW at N%% used (default: %(default)s)")
    args = parser.parse_args()

    conn = connect(args)
    project_id = conn.current_project_id
    if not project_id:
        raise SystemExit(
            "could not determine the authenticated OpenStack project ID")
    try:
        project_name = conn.current_project.name
    except (AttributeError, openstack.exceptions.SDKException):
        project_name = ""

    facts = {"project": {"name": project_name, "id": project_id,
                         "region": conn.config.get_region_name() or ""}}
    if not args.quotas:
        facts["images"] = gather_images(conn, project_id)
        facts["security_groups"] = gather_security_groups(conn, project_id)
        facts["networks"] = gather_networks(conn, project_id)
        facts["keypairs"] = gather_keypairs(conn)
    facts["quotas"] = gather_quotas(conn, project_id, args.warn_percent)

    if args.json:
        json.dump(facts, sys.stdout, indent=2, default=str)
        print()
        return 0

    project = facts["project"]
    print(f"OpenStack project: {project['name'] or '<unknown>'} ({project['id']})"
          + (f", region {project['region']}" if project["region"] else ""))
    if not args.quotas:
        print_table("Images", facts["images"],
                    ["name", "id", "access", "disk_format", "size_gb", "created"])
        print_table("Security groups", facts["security_groups"],
                    ["name", "id", "access", "rules", "description"])
        print_table("Networks", facts["networks"],
                    ["name", "id", "access", "status", "subnets"])
        print_table("Keypairs (per user, not per project)", facts["keypairs"],
                    ["name", "type", "fingerprint"])
    print_table("Quotas", facts["quotas"],
                ["service", "resource", "used_total", "free", "percent", "status"])
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        # Bad credentials, an unreachable Keystone, or a missing clouds.yaml
        # entry: one line, not a traceback.
        keystoneauth1.exceptions.ClientException,
        openstack.exceptions.SDKException,
    ) as error:
        raise SystemExit(f"openstack-project-facts: {error}")
