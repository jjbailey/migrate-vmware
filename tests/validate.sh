#!/bin/bash
# tests/validate.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Without set -e every check below could fail, be ignored, and the script
# would still print "Validation passed." and exit 0. Do not remove.
set -euo pipefail

repo_dir="$(CDPATH= builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." > /dev/null && builtin pwd -P)"
cd "$repo_dir"

failures=0

fail()
{
    echo "FAIL: $*" >&2
    failures=$((failures + 1))
}

echo "== ansible playbook syntax =="
for playbook in playbooks/*.yml ; do
    if ansible-playbook "$playbook" --syntax-check > /dev/null 2>&1 < /dev/null ; then
        echo "  ok   $playbook"
    else
        fail "$playbook failed --syntax-check"
    fi
done

echo "== shell syntax =="
while IFS= read -r script ; do
    if bash -n "$script" ; then
        echo "  ok   $script"
    else
        fail "$script failed bash -n"
    fi
done < <(find -L bin local playbooks/templates -type f \( -name '*.sh' -o -name '*.sh.j2' \) 2> /dev/null)

echo "== shellcheck =="
# SC1007 is the intentional `CDPATH= builtin cd` idiom. local/ holds the
# maintainer's lab test wrappers and is not held to this gate.
if command -v shellcheck > /dev/null 2>&1 ; then
    if shellcheck -S warning --exclude=SC1007 bin/*.sh ; then
        echo "  ok   bin/*.sh"
    else
        fail "shellcheck reported warnings in bin/"
    fi
    if shellcheck -S warning playbooks/templates/*.sh.j2 ; then
        echo "  ok   playbooks/templates/*.sh.j2"
    else
        fail "shellcheck reported warnings in playbooks/templates/"
    fi
else
    echo "  skip shellcheck (not installed)"
fi

echo "== doc file references =="
# Every repository path or script name a tracked Markdown file mentions must
# exist, so renamed or deleted files cannot linger in the docs.
if python3 - << 'PYTHON' ; then
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

# Paths the docs mention on purpose even though they are gone.
ALLOWED = {"vaults/env", "vaults/aws-staging.yml"}

tracked = [path for path in subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split()
           if Path(path).exists()]
dirs = {str(parent) + "/" for path in tracked for parent in Path(path).parents if str(parent) != "."}
basenames = {Path(path).name for path in tracked}
# The public export ships vaults/ and catalogs/ empty (see
# local/rsync-to-migrate-vmware.sh), so their files are only checked where
# they are tracked.
private_dirs = [d for d in ("vaults/", "catalogs/")
                if not any(path.startswith(d) and Path(path).name != ".gitignore" for path in tracked)]
path_ref = re.compile(r"(?<![\w./-])(?:\./)?((?:bin|local|playbooks|tests|doc|examples|catalogs|vaults)/[\w./*-]*)")
bare_ref = re.compile(r"`([\w.-]+\.(?:sh|py|yml|md))`")

rc = 0
for doc in sorted(path for path in tracked if path.endswith(".md")):
    for number, line in enumerate(Path(doc).read_text(encoding="utf-8").splitlines(), 1):
        refs = [m.group(1).rstrip(".") for m in path_ref.finditer(line)]
        for ref in refs:
            if ref in ALLOWED or ref.startswith(tuple(private_dirs)) or ref in dirs or ref + "/" in dirs or ref in tracked:
                continue
            if "*" in ref and fnmatch.filter(tracked, ref):
                continue
            print(f"  {doc}:{number}: {ref} is not tracked", file=sys.stderr)
            rc = 1
        for name in bare_ref.findall(line):
            if name not in basenames and not any(ref.endswith("/" + name) for ref in refs):
                print(f"  {doc}:{number}: {name} is not tracked", file=sys.stderr)
                rc = 1
sys.exit(rc)
PYTHON
    echo "  ok   Markdown file references"
else
    fail "Markdown docs reference files that do not exist"
fi

echo "== vaults =="
# The playbooks load every credential from these encrypted files; make sure
# the required ones exist and are actually vault-encrypted (plaintext here
# would both fail at runtime and risk leaking secrets into VCS).
vault_files=$(git ls-files 'vaults/*.yml')
if [ -z "$vault_files" ] ; then
    echo "  skip vaults (none tracked; public checkout)"
fi
for vault in $vault_files ; do
    if [ -s "$vault" ] && head -n 1 "$vault" | grep -q '^[$]ANSIBLE_VAULT;' ; then
        echo "  ok   $vault"
    else
        fail "$vault is missing, empty, or not an encrypted Ansible vault"
    fi
done

# Optional: decrypt the vsphere/openstack vaults and check the multi-vcenter /
# multi-project schema. Skipped silently when the vault password is missing so
# the gate still runs on machines without credentials.
vault_pass="${VAULT_PASSWORD_FILE:-$HOME/.ssh/dib-vault-pass}"
if [ -n "$vault_files" ] && [ -s "$vault_pass" ] && command -v ansible-vault > /dev/null 2>&1 ; then
    echo "== vault schema =="
    if python3 - "$vault_pass" << 'PYTHON' ; then
import subprocess
import json
import sys

import yaml

passfile = sys.argv[1]

def load(vault):
    out = subprocess.run(
        ["ansible-vault", "decrypt", "--vault-password-file", passfile, "-"],
        input=open(vault, encoding="utf-8").read(),
        capture_output=True, text=True, check=True,
    ).stdout
    return yaml.safe_load(out)

vsphere = load("vaults/vsphere.yml")
legacy_ok = all(str(vsphere.get(key) or "").strip()
                for key in ("vcenter_hostname", "vcenter_username", "vcenter_password"))
projects = vsphere.get("vsphere_projects") or {}
if not legacy_ok and not projects:
    raise SystemExit("vaults/vsphere.yml: neither the legacy flat vcenter_* "
                     "credentials nor vsphere_projects entries found")
for name, entry in projects.items():
    for key in ("vcenter_hostname", "vcenter_username", "vcenter_password"):
        if not str(entry.get(key) or "").strip():
            raise SystemExit(f"vaults/vsphere.yml: vsphere_projects[{name!r}] {key} missing or empty")

ostack = load("vaults/openstack.yml")
projects = ostack.get("openstack_projects") or {}
legacy = ostack.get("openstack_auth") or {}
if not projects and not legacy:
    raise SystemExit("vaults/openstack.yml: neither openstack_projects nor openstack_auth found")
for name, entry in projects.items():
    for key in ("auth_url", "username", "password", "project_name"):
        if not str(entry.get(key) or "").strip():
            raise SystemExit(f"vaults/openstack.yml: openstack_projects[{name!r}] {key} missing or empty")
    if str(entry.get("project_name")) != str(name):
        raise SystemExit(f"vaults/openstack.yml: openstack_projects[{name!r}] project_name disagrees with its key")
aws = load("vaults/aws.yml")
aws_projects = aws.get("aws_projects") or {}
if aws_projects:
    aws_entries = aws_projects.items()
else:
    aws_entries = [("legacy", aws)] if aws.get("aws_region") else []
if not aws_entries:
    raise SystemExit("vaults/aws.yml: neither aws_projects nor legacy aws_region found")
for name, entry in aws_entries:
    for key in ("aws_region", "s3_bucket", "vmimport_role_name"):
        if not str(entry.get(key) or "").strip():
            raise SystemExit(f"vaults/aws.yml: aws_projects[{name!r}] {key} missing or empty")
    account_id = entry.get("aws_account_id", entry.get("account_id"))
    if account_id is not None and not str(account_id).strip():
        raise SystemExit(f"vaults/aws.yml: aws_projects[{name!r}] account ID is empty")

gcp = load("vaults/gcp.yml")
gcp_projects = gcp.get("gcp_projects") or {}
if gcp_projects:
    gcp_entries = gcp_projects.items()
else:
    gcp_entries = [("legacy", gcp)] if gcp.get("gcp_project") else []
if not gcp_entries:
    raise SystemExit("vaults/gcp.yml: neither gcp_projects nor legacy gcp_project found")
for name, entry in gcp_entries:
    for key in ("gcp_project", "gcs_bucket", "gcp_import_location", "service_account_key"):
        if not str(entry.get(key) or "").strip():
            raise SystemExit(f"vaults/gcp.yml: gcp_projects[{name!r}] {key} missing or empty")
    if gcp_projects and str(entry.get("gcp_project")) != str(name):
        raise SystemExit(f"vaults/gcp.yml: gcp_projects[{name!r}] gcp_project disagrees with its key")
    try:
        client_email = json.loads(entry["service_account_key"]).get("client_email")
    except (TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"vaults/gcp.yml: gcp_projects[{name!r}] service_account_key is not valid JSON: {error}")
    if not str(client_email or "").strip():
        raise SystemExit(f"vaults/gcp.yml: gcp_projects[{name!r}] service_account_key.client_email missing or empty")
PYTHON
    echo "  ok   vault schemas (vsphere/openstack/aws/gcp)"
else
    fail "vault schema check failed"
fi
else
    echo "  skip vault schema check (no vault password or ansible-vault)"
fi

echo "== Python syntax =="
while IFS= read -r script ; do
    if python3 -m py_compile "$script" ; then
        echo "  ok   $script"
    else
        fail "$script failed py_compile"
    fi
done < <(find bin local tests -type f -name '*.py' 2> /dev/null)

echo "== catalogs =="
# append-migration-catalog.py / append-deployment-catalog.py are the
# gatekeepers for entry-level validation (required fields, non-empty
# strings, disk and image order) at write time. Run every row already on
# disk back through those same validators, so a hand edit or a row written
# before a validator was tightened is caught here rather than at launch.
if python3 - << 'PYTHON' ; then
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, "bin")

def validator(filename):
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_")[:-3], Path("bin") / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate

expected = {
    "migration-catalog.json": ("migrations", validator("append-migration-catalog.py")),
    "deployment-catalog.json": ("deployments", validator("append-deployment-catalog.py")),
}
rc = 0
for path in sorted(Path("catalogs").glob("*.json")) if Path("catalogs").is_dir() else []:
    if path.name not in expected:
        continue
    list_key, validate = expected[path.name]
    data = json.loads(path.read_text())
    if data.get("schema_version") != 1 or not isinstance(data.get(list_key), list):
        raise SystemExit(f"{path}: expected schema_version 1 and a {list_key} list")
    for position, entry in enumerate(data[list_key]):
        try:
            if not isinstance(entry, dict):
                raise ValueError("row is not an object")
            validate(entry)
        except ValueError as error:
            name = entry.get("vm_name", "<unknown>") if isinstance(entry, dict) else "<unknown>"
            print(f"  {path}: {list_key}[{position}] ({name}): {error}", file=sys.stderr)
            rc = 1
    print(f"  ok   {path}: {len(data[list_key])} row(s) checked")
sys.exit(rc)
PYTHON
    echo "  ok   catalogs (if present) pass the append validators"
else
    fail "catalog validation failed"
fi

echo "== yaml parse =="
if python3 - << 'PYTHON' ; then
import subprocess
import sys
from pathlib import Path

import yaml

# Tracked files plus untracked ones Git does not ignore, so an ignored
# .ansible/ collection cache or other local clutter is not parsed. Outside a
# Git checkout, fall back to a walk that skips hidden directories.
try:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True, check=True).stdout.decode().split("\0")
    candidates = [Path(name) for name in listed if name]
except (OSError, subprocess.CalledProcessError):
    candidates = [path for path in Path(".").rglob("*")
                  if not any(part.startswith(".") for part in path.parts)]
rc = 0
for path in sorted(path for path in candidates
                   if path.suffix in {".yml", ".yaml"} and path.is_file()):
    try:
        yaml.safe_load(path.read_text())
    except Exception as exc:
        print(f"  {path}: {exc}", file=sys.stderr)
        rc = 1
sys.exit(rc)
PYTHON
    echo "  ok   all yaml parsed"
else
    fail "YAML parse error"
fi

echo "== unit tests =="
# A test that skips (a missing nbdkit, qemu-img or ansible-playbook, say)
# still lets the run succeed, but say so: "ok" alone would read as though
# every test ran.
if python3 - << 'PYTHON' ; then
import sys
import unittest

suite = unittest.defaultTestLoader.discover("tests", pattern="test_*.py")
result = unittest.TextTestRunner().run(suite)
if result.skipped:
    reasons = sorted({reason for _, reason in result.skipped})
    print(f"  WARN {len(result.skipped)} unit test(s) skipped: {'; '.join(reasons)}")
sys.exit(0 if result.wasSuccessful() else 1)
PYTHON
    echo "  ok   focused unit tests"
    # After the tests, which re-create bin/ and tests/ caches by importing.
    rm -rf bin/__pycache__ local/__pycache__ tests/__pycache__
else
    fail "focused unit tests failed"
fi

echo "== Ansible collection versions =="
if python3 - << 'PYTHON' ; then
import re
import subprocess

import yaml

requirements = yaml.safe_load(open("requirements.yml", encoding="utf-8"))
result = subprocess.run(
    ["ansible-galaxy", "collection", "list"],
    check=True,
    capture_output=True,
    text=True,
)
for requirement in requirements.get("collections", []):
    name = requirement["name"]
    expected = str(requirement["version"])
    versions = re.findall(
        rf"^{re.escape(name)}\s+(\S+)\s*$",
        result.stdout,
        flags=re.MULTILINE,
    )
    if versions != [expected]:
        found = ", ".join(versions) if versions else "not installed"
        raise SystemExit(f"{name}: expected {expected}, found {found}")
PYTHON
    echo "  ok   installed collections match requirements.yml"
else
    fail "installed Ansible collections do not match requirements.yml"
fi

if [ "$failures" -ne 0 ] ; then
    echo "Validation FAILED: $failures problem(s)." >&2
    exit 1
fi

echo "Validation passed."
