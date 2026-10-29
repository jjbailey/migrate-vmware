#!/usr/bin/env python3
# bin/vault_common.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Shared Ansible Vault command and decryption helpers for project tools."""

import os
import shutil
import subprocess
from pathlib import Path

import yaml

def ansible_vault_bin():
    """Find ansible-vault in the configured virtualenv or on PATH."""
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
    """Decrypt a vault, using the configured or default password file."""
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
