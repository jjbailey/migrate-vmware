#!/bin/bash
# bin/edit-vault.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Edit (or create) one encrypted vault file.
#
# The per-target entry points -- aws-vault.sh, gcp-vault.sh,
# openstack-vault.sh, vsphere-vault.sh -- are symlinks to this
# script; the name it was invoked as selects vaults/<target>.yml. Set VAULT to
# edit some other file.
#
# ansible-vault comes from $DIB7_VENV_BIN (default ~/.dib7/bin) if present,
# else PATH. The vault password is looked up in this order:
# $VAULT_PASSWORD_FILE, then ~/.ssh/dib-vault-pass, then a prompt.

set -eu

script_dir="$(CDPATH= builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null && builtin pwd -P)"
project_dir="$(CDPATH= builtin cd -- "$script_dir/.." >/dev/null && builtin pwd -P)"

invoked_as="$(basename -- "$0")"
target="${invoked_as%-vault.sh}"
if [ "$target" = "edit" ] && [ -z "${VAULT:-}" ] ; then
    echo "$invoked_as: run this through <target>-vault.sh, or set VAULT" >&2
    exit 2
fi
if [ "$target" = "$invoked_as" ] || [ -z "$target" ] ; then
    if [ -z "${VAULT:-}" ] ; then
        echo "$invoked_as: run this through <target>-vault.sh, or set VAULT" >&2
        exit 2
    fi
    target=""
fi

VAULT="${VAULT:-vaults/$target.yml}"
# Prefer the dib7 venv's ansible-vault, else whatever is on PATH.
VENV_BIN="${DIB7_VENV_BIN:-$HOME/.dib7/bin}"
ANSIBLE_VAULT="$VENV_BIN/ansible-vault"
[ -x "$ANSIBLE_VAULT" ] || ANSIBLE_VAULT=$(command -v ansible-vault || true)

case "$VAULT" in
    /*) ;;
    *) VAULT="$project_dir/$VAULT" ;;
esac

[ -n "$ANSIBLE_VAULT" ] || { echo "ansible-vault not found in $VENV_BIN or on PATH" >&2; exit 1; }
[ -s "$VAULT" ] && action=edit || action=create

# Vault password lookup: explicit file, default file, then prompt.
pass_args=()
if [ -n "${VAULT_PASSWORD_FILE:-}" ] && [ -s "$VAULT_PASSWORD_FILE" ] ; then
    pass_args=(--vault-password-file "$VAULT_PASSWORD_FILE")
elif [ -s "$HOME/.ssh/dib-vault-pass" ] ; then
    pass_args=(--vault-password-file "$HOME/.ssh/dib-vault-pass")
fi

exec "$ANSIBLE_VAULT" "$action" --encrypt-vault-id=default "${pass_args[@]}" "$VAULT"
