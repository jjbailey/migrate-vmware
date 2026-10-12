#!/bin/bash
# bin/run-playbook.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Run an Ansible playbook from the repository using the local host tooling.
# Usage: ./bin/run-playbook.sh playbooks/<playbook.yml> [ansible-playbook args]
#
# bin/ansible-run.sh supplies the vault password from ~/.ssh/dib-vault-pass,
# VAULT_PASSWORD_FILE, or an interactive prompt.

set -euo pipefail

script_dir=$(CDPATH= builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)
repo_dir=$(CDPATH= builtin cd -- "$script_dir/.." > /dev/null && builtin pwd -P)

if [ "$#" -lt 1 ] ; then
    echo "Usage: $0 playbooks/<playbook.yml> [ansible-playbook args]" >&2
    exit 2
fi

cd "$repo_dir"
exec "$script_dir/ansible-run.sh" "$@"
