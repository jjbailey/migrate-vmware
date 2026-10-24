#!/bin/bash
# bin/migrate-openstack-dialog.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Dialog front end for bin/migrate-openstack-vm.sh. Nothing migrates until
# the final confirmation.

script_dir=$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)
cd "$script_dir/.." || exit
# shellcheck disable=SC2034 # read by launch-dialog-common.sh
DIALOG_NAME=migrate-openstack-dialog
# shellcheck disable=SC2034 # read by launch-dialog-common.sh
ACTION_VERB=Migration
# shellcheck source=launch-dialog-common.sh
source "$script_dir/launch-dialog-common.sh"
ARTIFACT_ROOT=${ARTIFACT_ROOT:-/var/tmp/migrate-vmware}

VSPHERE_TARGET_VCENTER=$(choose_target "Which source vCenter is this VM on?" vaults/vsphere.yml vsphere_projects "${VSPHERE_TARGET_VCENTER:-}") || abort
VCENTER_DATACENTER=""
if [ -z "$VSPHERE_TARGET_VCENTER" ] ; then
    VCENTER_DATACENTER=$(input "Legacy datacenter override (blank for the playbook default):" "${VCENTER_DATACENTER:-}") || abort
fi

VM_NAME=$(input "Source VM name in vCenter:" "${VM_NAME:-}") || abort
require_nonempty "$VM_NAME" "The VM name cannot be empty."

OS_PROJECT_NAME=$(choose_target "Which OpenStack project should this VM migrate into?" vaults/openstack.yml openstack_projects "${OS_PROJECT_NAME:-}") || abort

VALIDATE_CERTS=$(tristate "Override vCenter certificate validation?") || abort

if yesno "Quiesce the VM for a consistent snapshot?\n\nRecommended unless the guest doesn't support quiescing." yes ; then
    QUIESCE_SNAPSHOT=true
else
    QUIESCE_SNAPSHOT=false
fi

say "Migration summary for $VM_NAME

  Source vCenter.: ${VSPHERE_TARGET_VCENTER:-${VCENTER_DATACENTER:-auto-select}}
  OS project.....: ${OS_PROJECT_NAME:-auto-select}
  Validate certs.: ${VALIDATE_CERTS:-vault default}
  Quiesce snapshot: $QUIESCE_SNAPSHOT" 14

file=$(mktemp /tmp/migrate-openstack.XXXXXX)
{
    printf '#!/bin/bash\n'
    printf 'export VM_NAME=%s\n' "$(quote "$VM_NAME")"
    [ -n "$VSPHERE_TARGET_VCENTER" ] && printf 'export VSPHERE_TARGET_VCENTER=%s\n' "$(quote "$VSPHERE_TARGET_VCENTER")"
    [ -n "$VCENTER_DATACENTER" ] && printf 'export VCENTER_DATACENTER=%s\n' "$(quote "$VCENTER_DATACENTER")"
    [ -n "$OS_PROJECT_NAME" ] && printf 'export OS_PROJECT_NAME=%s\n' "$(quote "$OS_PROJECT_NAME")"
    [ -n "$VALIDATE_CERTS" ] && printf 'export VALIDATE_CERTS=%s\n' "$(quote "$VALIDATE_CERTS")"
    printf 'export ARTIFACT_ROOT=%s\n' "$(quote "$ARTIFACT_ROOT")"
    printf 'export QUIESCE_SNAPSHOT=%s\n' "$(quote "$QUIESCE_SNAPSHOT")"
    printf '%s\n' "$(quote "$script_dir/migrate-openstack-vm.sh")"
} > "$file"
chmod +x "$file"
finish "$file" "$VM_NAME" migrate
