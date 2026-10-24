#!/usr/bin/env python3
# bin/reconcile-catalog-openstack.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Remove OpenStack catalog entries when their resources are gone."""

import argparse
import json
import os
from pathlib import Path

from catalog_common import read_snapshot, remove_deployment_entries, remove_migration_entries

import keystoneauth1.exceptions
import openstack

def validate_openstack_environment(cloud):
    """Fail with actionable credentials guidance before connecting to OpenStack."""
    if cloud or os.environ.get("OS_CLOUD"):
        return

    required = {
        "OS_AUTH_URL": ("OS_AUTH_URL",),
        "OS_USERNAME or OS_USER_ID": ("OS_USERNAME", "OS_USER_ID"),
        "OS_PASSWORD or OS_TOKEN": ("OS_PASSWORD", "OS_TOKEN"),
        "OS_PROJECT_NAME or OS_PROJECT_ID": (
            "OS_PROJECT_NAME", "OS_PROJECT_ID"),
    }
    missing = [
        description for description, variables in required.items()
        if not any(os.environ.get(variable, "").strip() for variable in variables)
    ]
    if missing:
        raise ValueError(
            "missing required OpenStack environment variable(s): "
            + ", ".join(missing)
            + "; set the OS_* credentials or use --cloud with a configured "
            "clouds.yaml entry"
        )

def resource_exists(lookup, resource_id, cache):
    if resource_id in cache:
        return cache[resource_id]
    try:
        resource = lookup(resource_id)
    except openstack.exceptions.NotFoundException:
        resource = None
    cache[resource_id] = resource is not None
    return cache[resource_id]

def project_identity(entry):
    """All catalog identities an entry may carry (name and/or ID), as strings."""
    return {
        str(entry.get("openstack_project", "")),
        str(entry.get("openstack_project_id", "")),
    } - {""}

def in_project(entry, project_id):
    if isinstance(project_id, (set, frozenset, list, tuple)):
        return bool(project_identity(entry) & {str(item) for item in project_id})
    return str(project_id) in project_identity(entry)

def has_region(entry):
    recorded = entry.get("openstack_region")
    return isinstance(recorded, str) and bool(recorded.strip())

def in_region(entry, region_name):
    """Glance, Nova and Cinder IDs are per region; only check this region's rows.

    Rows written before openstack_region existed stay in scope, matching
    lookup-migration-openstack.
    """
    if region_name and has_region(entry):
        return entry.get("openstack_region") == region_name
    return True

def region_checkable(entry, region_name):
    """Whether a missing resource may remove this row in this region's check.

    A row with no openstack_region may belong to another region, where its
    resources would look missing from here, so a region-scoped check leaves
    it alone.
    """
    return in_region(entry, region_name) and (
        not region_name or has_region(entry))

def regionless_rows(migration_catalog, deployment_catalog, project_id, region_name):
    """Live rows in the project that a region-scoped check skips."""
    if not region_name:
        return []
    return [
        entry
        for catalog, list_key, live in (
            (migration_catalog, "migrations",
             lambda entry: entry.get("status") == "completed"),
            (deployment_catalog, "deployments",
             lambda entry: entry.get("status") != "retired"),
        )
        for entry in catalog[list_key]
        if entry.get("target_platform") == "openstack"
        and live(entry)
        and in_project(entry, project_id)
        and not has_region(entry)
    ]

def preferred_project_identity(requested_projects, migration_catalog, deployment_catalog):
    """Pick the requested identity to check against the authenticated project.

    A name/ID alias group may hold both; prefer one recorded as an
    openstack_project_id, because a non-admin user may not be able to read
    the current project's name.
    """
    recorded_ids = {
        str(entry.get("openstack_project_id"))
        for catalog, list_key in (
            (migration_catalog, "migrations"),
            (deployment_catalog, "deployments"),
        )
        for entry in catalog[list_key]
        if entry.get("target_platform") == "openstack"
        and entry.get("openstack_project_id")
    }
    return sorted(requested_projects & recorded_ids or requested_projects)[0]

def catalog_project_groups(migration_catalog, deployment_catalog):
    """Group name/ID aliases so one project is not counted twice."""
    groups = []
    for catalog, list_key in ((migration_catalog, "migrations"), (deployment_catalog, "deployments")):
        for entry in catalog[list_key]:
            if entry.get("target_platform") != "openstack":
                continue
            identities = project_identity(entry)
            if not identities:
                continue
            for group in groups:
                if group & identities:
                    group.update(identities)
                    break
            else:
                groups.append(set(identities))
    return groups

def catalog_scope(migration_catalog, deployment_catalog):
    projects = set()
    regions = set()
    for catalog, list_key in (
        (migration_catalog, "migrations"),
        (deployment_catalog, "deployments"),
    ):
        for entry in catalog[list_key]:
            if entry.get("target_platform") != "openstack":
                continue
            identities = project_identity(entry)
            if not identities:
                continue
            projects.update(identities)
            region = entry.get("openstack_region")
            if region:
                regions.add(region)
    return projects, regions

def validate_scope(
    connection, migration_catalog, deployment_catalog, requested_project_id,
    region_name
):
    # One run reconciles one project; the catalogs may hold many. Entries for
    # other projects/regions are left alone, not treated as an error.
    project_id = connection.current_project_id
    if not project_id:
        raise ValueError(
            "could not determine the authenticated OpenStack project ID"
        )
    authenticated_identities = {str(project_id)}
    try:
        project_name = connection.current_project.name
    except (AttributeError, TypeError, openstack.exceptions.SDKException):
        project_name = None
    if project_name:
        authenticated_identities.add(str(project_name))
    if requested_project_id and str(requested_project_id) not in authenticated_identities:
        raise ValueError(
            f"authenticated project {project_id} does not match "
            f"--project-id {requested_project_id}"
        )
    projects, catalog_regions = catalog_scope(
        migration_catalog, deployment_catalog
    )
    scope_identities = authenticated_identities & {
        str(identity) for identity in projects}
    if not scope_identities:
        raise ValueError(
            f"catalog contains no OpenStack entries for authenticated "
            f"project {project_id}"
        )
    project_regions = {
        entry.get("openstack_region")
        for catalog, list_key in (
            (migration_catalog, "migrations"),
            (deployment_catalog, "deployments"),
        )
        for entry in catalog[list_key]
        if entry.get("target_platform") == "openstack"
        and in_project(entry, scope_identities)
        and entry.get("openstack_region")
    }
    if region_name not in project_regions:
        raise ValueError(
            f"project {project_id} has no catalog entries in region "
            f"{region_name}; found: {', '.join(sorted(project_regions)) or '<none>'}"
        )
    return scope_identities

def candidate_count(migration_catalog, deployment_catalog, project_id=None, region_name=None):
    migrations = sum(
        1
        for entry in migration_catalog["migrations"]
        if entry.get("target_platform") == "openstack"
        and entry.get("status") == "completed"
        and (project_id is None or in_project(entry, project_id))
        and region_checkable(entry, region_name)
    )
    deployments = sum(
        1
        for entry in deployment_catalog["deployments"]
        if entry.get("target_platform") == "openstack"
        and entry.get("status") != "retired"
        and (project_id is None or in_project(entry, project_id))
        and region_checkable(entry, region_name)
    )
    return migrations + deployments

def find_missing_resources(connection, migration_catalog, deployment_catalog, project_id=None, region_name=None):
    image_cache = {}
    server_cache = {}
    volume_cache = {}
    port_cache = {}
    stale_migrations = []
    stale_deployments = []

    for entry in migration_catalog["migrations"]:
        if (
            entry.get("target_platform") != "openstack"
            or entry.get("status") != "completed"
        ):
            continue
        if project_id is not None and not in_project(entry, project_id):
            continue
        if not region_checkable(entry, region_name):
            continue
        image_ids = entry.get("openstack_image_ids")
        if not isinstance(image_ids, list) or not image_ids:
            raise ValueError(
                f"OpenStack migration {entry.get('clone_name', '<unknown>')} "
                "has no openstack_image_ids list"
            )
        missing = [
            image_id
            for image_id in image_ids
            if not resource_exists(
                connection.get_image, image_id, image_cache
            )
        ]
        if missing:
            stale_migrations.append((entry, missing))

    for entry in deployment_catalog["deployments"]:
        if (
            entry.get("target_platform") != "openstack"
            or entry.get("status") == "retired"
        ):
            continue
        if project_id is not None and not in_project(entry, project_id):
            continue
        if not region_checkable(entry, region_name):
            continue
        server_id = entry.get("openstack_server_id")
        if not server_id:
            raise ValueError(
                f"OpenStack deployment for {entry.get('vm_name', '<unknown>')} "
                "has no openstack_server_id"
            )
        missing_server = not resource_exists(
            connection.get_server, server_id, server_cache
        )
        volume_ids = entry.get("openstack_data_volume_ids", [])
        if not isinstance(volume_ids, list):
            raise ValueError(
                f"OpenStack deployment {server_id} has invalid "
                "openstack_data_volume_ids"
            )
        missing_volumes = [
            volume_id
            for volume_id in volume_ids
            if not resource_exists(
                connection.get_volume, volume_id, volume_cache
            )
        ]
        port_ids = entry.get("openstack_port_ids") or []
        if not isinstance(port_ids, list):
            raise ValueError(
                f"OpenStack deployment {server_id} has invalid "
                "openstack_port_ids"
            )
        if not port_ids:
            nics = entry.get("openstack_nics") or []
            if isinstance(nics, list):
                port_ids = [
                    nic.get("port-id")
                    for nic in nics
                    if isinstance(nic, dict) and nic.get("port-id")
                ]
        orphan_ports = []
        if missing_server and port_ids:
            for port_id in port_ids:
                if not isinstance(port_id, str) or not port_id.strip():
                    continue
                if resource_exists(connection.get_port, port_id, port_cache):
                    orphan_ports.append(port_id)
        if missing_server or missing_volumes:
            stale_deployments.append(
                (entry, missing_server, missing_volumes, orphan_ports)
            )

    return stale_migrations, stale_deployments

def describe_migration(entry, missing):
    return (
        f"removing migration {entry.get('vm_name', '<unknown>')} "
        f"{entry.get('clone_name', '<unknown>')} "
        f"[{entry.get('openstack_project', '<unknown>')}]: missing Glance image(s) "
        + ", ".join(missing)
    )

def describe_deployment(entry, missing_server, missing_volumes, orphan_ports=None):
    missing = []
    if missing_server:
        missing.append(
            f"server {entry.get('openstack_server_id', '<unknown>')}"
        )
    if missing_volumes:
        missing.append("volume(s) " + ", ".join(missing_volumes))
    note = ""
    if orphan_ports:
        note = (
            "; orphan network-identity port(s) still present: "
            + ", ".join(orphan_ports)
            + " (delete manually before reusing their MACs or fixed IPs)"
        )
    return (
        f"removing deployment {entry.get('vm_name', '<unknown>')} "
        f"{entry.get('openstack_server_id', '<unknown>')} "
        f"[{entry.get('openstack_project', '<unknown>')}]: missing "
        + "; ".join(missing)
        + note
    )

def main():
    repo_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migration-catalog",
        type=Path,
        default=repo_dir / "catalogs/migration-catalog.json",
    )
    parser.add_argument(
        "--deployment-catalog",
        type=Path,
        default=repo_dir / "catalogs/deployment-catalog.json",
    )
    parser.add_argument(
        "--cloud",
        help="named clouds.yaml entry; otherwise use standard OpenStack settings",
    )
    parser.add_argument(
        "--region-name",
        help="OpenStack region; inferred when the catalogs contain one region",
    )
    parser.add_argument("--project-id")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report stale entries without writing either catalog",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow removal of more than half of the scoped entries",
    )
    args = parser.parse_args()

    migration_catalog = read_snapshot(args.migration_catalog, "migrations")
    deployment_catalog = read_snapshot(args.deployment_catalog, "deployments")
    projects, _ = catalog_scope(migration_catalog, deployment_catalog)
    project_groups = catalog_project_groups(
        migration_catalog, deployment_catalog)
    if args.project_id:
        requested_projects = {str(args.project_id)}
        unknown = requested_projects - {str(identity) for identity in projects}
        if unknown:
            raise ValueError(
                f"catalog contains no OpenStack entries for project(s) "
                f"{', '.join(sorted(unknown))}"
            )
    elif len(project_groups) == 1:
        requested_projects = project_groups[0]
    elif not project_groups:
        raise ValueError(
            "catalog contains no OpenStack entries with a project identity; "
            "provide --project-id"
        )
    else:
        raise ValueError(
            "catalog contains multiple OpenStack projects "
            f"({', '.join(sorted(projects))}); reconcile one project per run "
            "with --project-id, scoping --cloud/standard auth env to that project"
        )
    if args.region_name:
        region_name = args.region_name
    else:
        project_regions = {
            entry.get("openstack_region")
            for catalog, list_key in (
                (migration_catalog, "migrations"),
                (deployment_catalog, "deployments"),
            )
            for entry in catalog[list_key]
            if entry.get("target_platform") == "openstack"
            and project_identity(entry) & requested_projects
            and entry.get("openstack_region")
        }
        if len(project_regions) == 1:
            region_name = next(iter(project_regions))
        elif len(project_regions) > 1:
            raise ValueError(
                "catalog contains multiple OpenStack regions for project(s) "
                f"{', '.join(sorted(requested_projects))}; provide --region-name"
            )
        else:
            raise ValueError(
                "catalog contains no OpenStack region; provide --region-name"
            )
    # One run reconciles one project (see --project-id handling above): a
    # single openstack connection is scoped to one project's auth, so one
    # invocation can only check that project's resources.
    project_id = preferred_project_identity(
        requested_projects, migration_catalog, deployment_catalog)
    validate_openstack_environment(args.cloud)
    connect_args = {}
    if args.cloud:
        connect_args["cloud"] = args.cloud
    connect_args["region_name"] = region_name
    connection = openstack.connect(**connect_args)
    authenticated_identities = validate_scope(
        connection,
        migration_catalog,
        deployment_catalog,
        project_id,
        region_name,
    )
    print(
        f"OpenStack scope: project_id={connection.current_project_id}, "
        f"region={region_name}"
    )
    stale_migrations, stale_deployments = find_missing_resources(
        connection, migration_catalog, deployment_catalog, authenticated_identities,
        region_name
    )
    # Retired rows go by status, not by a resource lookup, so a row with no
    # openstack_region is safe to remove here.
    retired_migrations = [
        entry
        for entry in migration_catalog["migrations"]
        if entry.get("target_platform") == "openstack"
        and entry.get("status") == "retired"
        and in_project(entry, authenticated_identities)
        and in_region(entry, region_name)
    ]
    retired_deployments = [
        entry
        for entry in deployment_catalog["deployments"]
        if entry.get("target_platform") == "openstack"
        and entry.get("status") == "retired"
        and in_project(entry, authenticated_identities)
        and in_region(entry, region_name)
    ]

    skipped = regionless_rows(
        migration_catalog, deployment_catalog, authenticated_identities,
        region_name)
    for entry in skipped:
        print(
            f"skipping {entry.get('vm_name', '<unknown>')} "
            f"{entry.get('clone_name') or entry.get('openstack_server_id') or '<unknown>'}: "
            "no openstack_region recorded, so it is not checked or removed "
            f"from region {region_name}"
        )
    for entry, missing in stale_migrations:
        print(describe_migration(entry, missing))
    for entry, missing_server, missing_volumes, orphan_ports in stale_deployments:
        print(describe_deployment(
            entry, missing_server, missing_volumes, orphan_ports))
    for entry in retired_migrations:
        print(
            f"removing previously retired migration "
            f"{entry.get('vm_name', '<unknown>')} "
            f"{entry.get('clone_name', '<unknown>')}"
        )
    for entry in retired_deployments:
        print(
            f"removing previously retired deployment "
            f"{entry.get('vm_name', '<unknown>')} "
            f"{entry.get('openstack_server_id', '<unknown>')}"
        )

    stale_total = len(stale_migrations) + len(stale_deployments)
    total = (
        stale_total
        + len(retired_migrations)
        + len(retired_deployments)
    )
    if not total:
        print("no stale OpenStack catalog entries found")
        return 0
    if args.dry_run:
        print(
            f"{total} entries would be removed "
            "(--dry-run, catalogs not written)"
        )
        return 0
    scope_total = candidate_count(
        migration_catalog, deployment_catalog, authenticated_identities,
        region_name)
    if (
        stale_total
        and scope_total
        and stale_total * 2 > scope_total
        and not args.force
    ):
        raise ValueError(
            f"refusing to remove {stale_total} of {scope_total} "
            "scoped entries; inspect --dry-run and rerun with --force "
            "if this is intentional"
        )

    migration_changed = remove_migration_entries(
        args.migration_catalog, stale_migrations, retired_migrations, "openstack", "clone_name", region_name, authenticated_identities
    )
    deployment_changed = remove_deployment_entries(
        args.deployment_catalog, stale_deployments, retired_deployments, "openstack", "openstack_server_id", region_name, authenticated_identities
    )
    print(
        f"removed {migration_changed} migration and "
        f"{deployment_changed} deployment entries"
    )
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        openstack.exceptions.SDKException,
        keystoneauth1.exceptions.ClientException,
    ) as error:
        raise SystemExit(f"reconcile-catalog-openstack: {error}")
