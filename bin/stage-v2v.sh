#!/bin/bash
# bin/stage-v2v.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Run playbooks/stage-v2v.yml against one VM.
#
# The VM is given to ansible as a one-host [migrate] inventory, so its name is
# the inventory_hostname the playbook uses for the vCenter lookup. SSH connects
# to that name too, unless -a gives an address (for VMs whose vCenter name is
# not in DNS). bin/ansible-run.sh supplies the vault password.

set -euo pipefail

script_dir=$(CDPATH= builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)
repo_dir=$(CDPATH= builtin cd -- "$script_dir/.." > /dev/null && builtin pwd -P)

usage()
{
    cat << EOF
Usage: $(basename -- "$0") -c vcenter -H vm [-a address] [-u ssh_username] [-k ssh_private_key_file] [-- ansible-playbook args]

  -c vcenter   vCenter hostname (or vaults/vsphere.yml key) to use
  -H vm        VM name, as it appears in vCenter
  -a address   address to SSH to (default: the VM name, which must resolve)
  -u username  SSH username (default: from the vault, else ansible's default)
  -k keyfile   SSH private key file (default: from the vault, else ssh-agent)
EOF
    exit "${1:-2}"
}

vcenter_hostname=""
inventory_hostname=""
ssh_address=""
ssh_username=""
ssh_private_key_file=""

while getopts ":c:H:a:u:k:h" opt ; do
    case "$opt" in
        c) vcenter_hostname="$OPTARG" ;;
        H) inventory_hostname="$OPTARG" ;;
        a) ssh_address="$OPTARG" ;;
        u) ssh_username="$OPTARG" ;;
        k) ssh_private_key_file="$OPTARG" ;;
        h) usage 0 ;;
        *) usage ;;
    esac
done
shift $((OPTIND - 1))

if [ -z "$vcenter_hostname" ] || [ -z "$inventory_hostname" ] ; then
    usage
fi

# The name goes into an INI inventory line, where whitespace would split it.
case "$inventory_hostname$ssh_address" in
    *[[:space:]]*)
        echo "$(basename -- "$0"): VM name and address must not contain whitespace" >&2
        exit 2
        ;;
esac

inventory=$(mktemp)
trap 'rm -f "$inventory"' EXIT
if [ -n "$ssh_address" ] ; then
    printf '[migrate]\n%s ansible_host=%s\n' "$inventory_hostname" "$ssh_address" > "$inventory"
else
    printf '[migrate]\n%s\n' "$inventory_hostname" > "$inventory"
fi

# The playbook matches vsphere_target_vcenter against a vault key or a
# vcenter_hostname, so the vCenter goes in under that name.
extra_vars=(-e "vsphere_target_vcenter=$vcenter_hostname")
if [ -n "$ssh_username" ] ; then
    extra_vars+=(-e "ssh_username=$ssh_username")
fi
if [ -n "$ssh_private_key_file" ] ; then
    extra_vars+=(-e "ssh_private_key_file=$ssh_private_key_file")
fi

cd "$repo_dir"
# No exec: the EXIT trap has to remove the temporary inventory.
"$script_dir/ansible-run.sh" playbooks/stage-v2v.yml -i "$inventory" "${extra_vars[@]}" "$@"
