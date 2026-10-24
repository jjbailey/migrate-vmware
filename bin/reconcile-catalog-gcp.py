#!/usr/bin/env python3
# bin/reconcile-catalog-gcp.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Remove GCP catalog entries when their resources are gone."""

import argparse
import json
from pathlib import Path

from catalog_common import read_snapshot, remove_deployment_entries, remove_migration_entries

import google.auth
import google.auth.exceptions
from google.auth.transport.requests import AuthorizedSession

COMPUTE_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
COMPUTE_API = "https://compute.googleapis.com/compute/v1/projects"

def catalog_scope(migration_catalog, deployment_catalog):
    projects = set()
    for catalog, list_key in ((migration_catalog, "migrations"), (deployment_catalog, "deployments")):
        for entry in catalog[list_key]:
            if entry.get("target_platform") != "gcp":
                continue
            project = entry.get("gcp_project")
            if not isinstance(project, str) or not project.strip():
                continue
            projects.add(project)
    return projects

def validate_scope(migration_catalog, deployment_catalog, project_id):
    projects = catalog_scope(migration_catalog, deployment_catalog)
    if project_id not in projects:
        raise ValueError(
            f"catalog contains no GCP entries for project {project_id}")

def get_resource(session, url, cache):
    if url in cache:
        return cache[url]
    response = session.get(url)
    if response.status_code == 404:
        resource = None
    else:
        response.raise_for_status()
        resource = response.json()
    cache[url] = resource
    return resource

def find_missing_resources(session, project_id, migration_catalog, deployment_catalog):
    image_cache, instance_cache, disk_cache = {}, {}, {}
    stale_migrations, stale_deployments = [], []

    for entry in migration_catalog["migrations"]:
        if entry.get("target_platform") != "gcp" or entry.get("status") != "completed":
            continue
        if entry.get("gcp_project") != project_id:
            continue
        image_names = [entry.get("gcp_boot_image_name"),
                       *entry.get("gcp_data_image_names", [])]
        if not image_names or any(not isinstance(name, str) or not name.strip() for name in image_names):
            raise ValueError(
                f"GCP migration {entry.get('clone_name', '<unknown>')} has invalid GCP image names")
        missing = []
        for image_name in image_names:
            url = f"{COMPUTE_API}/{project_id}/global/images/{image_name}"
            if get_resource(session, url, image_cache) is None:
                missing.append(image_name)
        if missing:
            stale_migrations.append((entry, missing))

    for entry in deployment_catalog["deployments"]:
        if entry.get("target_platform") != "gcp" or entry.get("status") == "retired":
            continue
        if entry.get("gcp_project") != project_id:
            continue
        instance_name, zone = entry.get(
            "gcp_instance_name"), entry.get("gcp_zone")
        if not instance_name or not zone:
            raise ValueError(
                f"GCP deployment for {entry.get('vm_name', '<unknown>')} has no gcp_instance_name or gcp_zone")
        instance_url = f"{COMPUTE_API}/{project_id}/zones/{zone}/instances/{instance_name}"
        missing_instance = get_resource(
            session, instance_url, instance_cache) is None
        disk_names = entry.get("gcp_data_disk_names", [])
        if not isinstance(disk_names, list):
            raise ValueError(
                f"GCP deployment {instance_name} has invalid gcp_data_disk_names")
        missing_disks = []
        for disk_name in disk_names:
            disk_url = f"{COMPUTE_API}/{project_id}/zones/{zone}/disks/{disk_name}"
            if get_resource(session, disk_url, disk_cache) is None:
                missing_disks.append(disk_name)
        if missing_instance or missing_disks:
            stale_deployments.append((entry, missing_instance, missing_disks))
    return stale_migrations, stale_deployments

def candidate_count(migration_catalog, deployment_catalog, project_id):
    return sum(1 for entry in migration_catalog["migrations"] if entry.get("target_platform") == "gcp" and entry.get("status") == "completed" and entry.get("gcp_project") == project_id) + sum(1 for entry in deployment_catalog["deployments"] if entry.get("target_platform") == "gcp" and entry.get("status") != "retired" and entry.get("gcp_project") == project_id)

def main():
    repo_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-catalog", type=Path,
                        default=repo_dir / "catalogs/migration-catalog.json")
    parser.add_argument("--deployment-catalog", type=Path,
                        default=repo_dir / "catalogs/deployment-catalog.json")
    parser.add_argument(
        "--project-id",
        help="GCP project; inferred when the catalogs contain one project",
    )
    parser.add_argument("--credentials-file", type=Path,
                        help="service-account JSON; otherwise use standard Google credentials")
    parser.add_argument("--dry-run", action="store_true",
                        help="report stale entries without writing either catalog")
    parser.add_argument("--force", action="store_true",
                        help="allow removal of more than half of the scoped entries")
    args = parser.parse_args()

    migration_catalog = read_snapshot(args.migration_catalog, "migrations")
    deployment_catalog = read_snapshot(args.deployment_catalog, "deployments")
    projects = catalog_scope(migration_catalog, deployment_catalog)
    if args.project_id:
        project_id = args.project_id
    elif len(projects) == 1:
        project_id = next(iter(projects))
    else:
        raise ValueError(
            "catalog contains multiple GCP projects; provide --project-id"
        )
    validate_scope(migration_catalog, deployment_catalog, project_id)
    credential_args = {"scopes": [COMPUTE_SCOPE]}
    if args.credentials_file:
        credentials, credential_project = google.auth.load_credentials_from_file(
            args.credentials_file, **credential_args)
        credential_project = credential_project or getattr(credentials, "project_id", None)
    else:
        # google.auth.default() reports the ambient default project (gcloud
        # config, GOOGLE_CLOUD_PROJECT), which says nothing about where the
        # credentials live or what they can reach. Only a project the
        # credentials themselves carry, as a service account does, is a
        # reason to refuse.
        credentials, _ = google.auth.default(**credential_args)
        credential_project = getattr(credentials, "project_id", None)
    if credential_project and str(credential_project) != str(project_id):
        raise ValueError(
            f"resolved GCP credentials belong to project {credential_project}, "
            f"not --project-id {project_id}")
    session = AuthorizedSession(credentials)

    print(f"GCP scope: project_id={project_id}")
    stale_migrations, stale_deployments = find_missing_resources(
        session, project_id, migration_catalog, deployment_catalog)
    retired_migrations = [entry for entry in migration_catalog["migrations"] if entry.get("target_platform") == "gcp" and entry.get("status") == "retired" and entry.get("gcp_project") == project_id]
    retired_deployments = [entry for entry in deployment_catalog["deployments"] if entry.get("target_platform") == "gcp" and entry.get("status") == "retired" and entry.get("gcp_project") == project_id]

    for entry, missing in stale_migrations:
        print(
            f"removing migration {entry.get('vm_name', '<unknown>')} {entry.get('clone_name', '<unknown>')}: missing image(s) {', '.join(missing)}")
    for entry, missing_instance, missing_disks in stale_deployments:
        missing = (
            [f"instance {entry.get('gcp_instance_name', '<unknown>')}"] if missing_instance else [])
        if missing_disks:
            missing.append("disk(s) " + ", ".join(missing_disks))
        print(
            f"removing deployment {entry.get('vm_name', '<unknown>')} {entry.get('gcp_instance_name', '<unknown>')}: missing {'; '.join(missing)}")
    for entry in retired_migrations:
        print(
            f"removing previously retired migration {entry.get('vm_name', '<unknown>')} {entry.get('clone_name', '<unknown>')}")
    for entry in retired_deployments:
        print(
            f"removing previously retired deployment {entry.get('vm_name', '<unknown>')} {entry.get('gcp_instance_name', '<unknown>')}")

    stale_total = len(stale_migrations) + len(stale_deployments)
    total = stale_total + len(retired_migrations) + len(retired_deployments)
    if not total:
        print("no stale GCP catalog entries found")
        return 0
    if args.dry_run:
        print(f"{total} entries would be removed (--dry-run, catalogs not written)")
        return 0
    scope_total = candidate_count(migration_catalog, deployment_catalog, project_id)
    if stale_total and scope_total and stale_total * 2 > scope_total and not args.force:
        raise ValueError(
            f"refusing to remove {stale_total} of {scope_total} scoped entries; inspect --dry-run and rerun with --force if this is intentional")
    migration_changed = remove_migration_entries(
        args.migration_catalog, stale_migrations, retired_migrations,
        "gcp", "clone_name", project_id=project_id)
    deployment_changed = remove_deployment_entries(
        args.deployment_catalog, stale_deployments, retired_deployments,
        "gcp", "gcp_instance_name", project_id=project_id)
    print(
        f"removed {migration_changed} migration and {deployment_changed} deployment entries")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        # Missing, unreadable or expired credentials (DefaultCredentialsError,
        # RefreshError): one line, not a traceback.
        google.auth.exceptions.GoogleAuthError,
    ) as error:
        raise SystemExit(f"reconcile-catalog-gcp: {error}")
