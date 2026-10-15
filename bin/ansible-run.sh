#!/bin/bash
# bin/ansible-run.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Wrapper to run ansible-playbook with the vault password wired up.
#
# All credentials live in the encrypted files in vaults/, so ansible needs
# the vault password to load them. This wrapper
# appends one of, in order:
#   1. nothing, if you already passed --ask-vault-pass, --vault-password-file,
#      or --vault-id
#   2. --vault-password-file $VAULT_PASSWORD_FILE, if that env var points at
#      a non-empty file (the value is a path, not the secret itself)
#   3. --vault-password-file ~/.ssh/dib-vault-pass, if that file exists
#   4. --ask-vault-pass (interactive prompt)
set -euo pipefail

DEFAULT_VAULT_PASSWORD_FILE="$HOME/.ssh/dib-vault-pass"

usage()
{
    cat << EOF
Usage: ansible-run playbook.yml [ansible-playbook args...]

Appends a vault-password option (see header comment) unless one was already
given, then execs ansible-playbook.
EOF
    exit 2
}

# require at least one arg (the playbook)
if [ $# -lt 1 ] ; then
    usage
fi

ARGS=("$@")
has_vault_option=false
for arg in "${ARGS[@]}" ; do
    case "$arg" in
        --ask-vault-pass | --ask-vault-password | -J | \
            --vault-password-file | --vault-password-file=* | \
            --vault-pass-file | --vault-pass-file=* | \
            --vault-id | --vault-id=*)
            has_vault_option=true
            break
            ;;
    esac
done

if [ "$has_vault_option" = true ] ; then
    : # vault password handling already on the command line
elif [ -n "${VAULT_PASSWORD_FILE:-}" ] && [ -s "${VAULT_PASSWORD_FILE}" ] ; then
    ARGS+=("--vault-password-file" "${VAULT_PASSWORD_FILE}")
elif [ -s "${DEFAULT_VAULT_PASSWORD_FILE}" ] ; then
    ARGS+=("--vault-password-file" "${DEFAULT_VAULT_PASSWORD_FILE}")
else
    ARGS+=("--ask-vault-pass")
fi

exec ansible-playbook "${ARGS[@]}"
