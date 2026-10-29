#!/usr/bin/env python3
# tests/check_vault_schema.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Validate the required credential shapes in the encrypted vault files."""

import json
import subprocess
import sys
from pathlib import Path

import yaml

def load_vault(passfile, vault):
    """Decrypt one vault using the supplied password file."""
    result = subprocess.run(
        ["ansible-vault", "decrypt", "--vault-password-file", str(passfile), "-"],
        input=vault.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        check=True,
    )
    return yaml.safe_load(result.stdout) or {}

def require_entry(entries, label, required):
    for name, entry in entries:
        for key in required:
            if not str(entry.get(key) or "").strip():
                raise ValueError(f"{label}[{name!r}] {key} missing or empty")

def validate_vaults(passfile, repo_dir):
    def load(name):
        return load_vault(passfile, repo_dir / "vaults" / f"{name}.yml")

    vsphere = load("vsphere")
    legacy_ok = all(str(vsphere.get(key) or "").strip()
                    for key in ("vcenter_hostname", "vcenter_username", "vcenter_password"))
    projects = vsphere.get("vsphere_projects") or {}
    if not legacy_ok and not projects:
        raise ValueError(
            "vaults/vsphere.yml: neither the legacy flat vcenter_* "
            "credentials nor vsphere_projects entries found")
    require_entry(projects.items(), "vaults/vsphere.yml: vsphere_projects",
                  ("vcenter_hostname", "vcenter_username", "vcenter_password"))

    openstack = load("openstack")
    projects = openstack.get("openstack_projects") or {}
    legacy = openstack.get("openstack_auth") or {}
    if not projects and not legacy:
        raise ValueError(
            "vaults/openstack.yml: neither openstack_projects nor openstack_auth found")
    require_entry(projects.items(), "vaults/openstack.yml: openstack_projects",
                  ("auth_url", "username", "password", "project_name"))
    for name, entry in projects.items():
        if str(entry.get("project_name")) != str(name):
            raise ValueError(
                f"vaults/openstack.yml: openstack_projects[{name!r}] "
                "project_name disagrees with its key")

    aws = load("aws")
    projects = aws.get("aws_projects") or {}
    entries = projects.items() if projects else (
        [("legacy", aws)] if aws.get("aws_region") else [])
    if not entries:
        raise ValueError(
            "vaults/aws.yml: neither aws_projects nor legacy aws_region found")
    require_entry(entries, "vaults/aws.yml: aws_projects",
                  ("aws_region", "s3_bucket", "vmimport_role_name"))
    for name, entry in entries:
        account_id = entry.get("aws_account_id", entry.get("account_id"))
        if account_id is not None and not str(account_id).strip():
            raise ValueError(
                f"vaults/aws.yml: aws_projects[{name!r}] account ID is empty")

    gcp = load("gcp")
    projects = gcp.get("gcp_projects") or {}
    entries = projects.items() if projects else (
        [("legacy", gcp)] if gcp.get("gcp_project") else [])
    if not entries:
        raise ValueError(
            "vaults/gcp.yml: neither gcp_projects nor legacy gcp_project found")
    require_entry(entries, "vaults/gcp.yml: gcp_projects",
                  ("gcp_project", "gcs_bucket", "gcp_import_location", "service_account_key"))
    for name, entry in entries:
        if projects and str(entry.get("gcp_project")) != str(name):
            raise ValueError(
                f"vaults/gcp.yml: gcp_projects[{name!r}] "
                "gcp_project disagrees with its key")
        try:
            client_email = json.loads(entry["service_account_key"]).get("client_email")
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError(
                f"vaults/gcp.yml: gcp_projects[{name!r}] service_account_key "
                f"is not valid JSON: {error}") from error
        if not str(client_email or "").strip():
            raise ValueError(
                f"vaults/gcp.yml: gcp_projects[{name!r}] "
                "service_account_key.client_email missing or empty")

def main():
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} VAULT_PASSWORD_FILE")
    passfile = Path(sys.argv[1])
    repo_dir = Path(__file__).resolve().parents[1]
    try:
        validate_vaults(passfile, repo_dir)
    except (OSError, subprocess.CalledProcessError, ValueError, yaml.YAMLError) as error:
        raise SystemExit(f"vault schema check failed: {error}") from error

if __name__ == "__main__":
    main()
