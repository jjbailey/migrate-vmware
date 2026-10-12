#!/bin/bash
# bin/migrate-aws-vm.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

set -euo pipefail
wrapper_name=migrate-aws-vm
export wrapper_name
wrapper_extra_args=()
# shellcheck source=wrapper-common.sh
# shellcheck disable=SC1091
source "$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)/wrapper-common.sh"
wrapper_capture_args "$@"

VSPHERE_TARGET_VCENTER=${VSPHERE_TARGET_VCENTER:-}
VCENTER_DATACENTER=${VCENTER_DATACENTER:-}
VM_NAME=${VM_NAME:-}
AWS_TARGET_PROJECT=${AWS_TARGET_PROJECT:-}
VALIDATE_CERTS=${VALIDATE_CERTS:-}
QUIESCE_SNAPSHOT=${QUIESCE_SNAPSHOT:-true}
ARTIFACT_ROOT=${ARTIFACT_ROOT:-/work/migrate-vmware}

wrapper_require_bool VALIDATE_CERTS "$VALIDATE_CERTS"
wrapper_require_bool QUIESCE_SNAPSHOT "$QUIESCE_SNAPSHOT"

args=()
[ -n "$VM_NAME" ] && args+=(-e "vm_name=$VM_NAME")
[ -n "$AWS_TARGET_PROJECT" ] && args+=(-e "aws_target_project=$AWS_TARGET_PROJECT")
[ -n "$VSPHERE_TARGET_VCENTER" ] && args+=(-e "vsphere_target_vcenter=$VSPHERE_TARGET_VCENTER")
if [ -z "$VSPHERE_TARGET_VCENTER" ] && [ -n "$VCENTER_DATACENTER" ] ; then
    args+=(-e "datacenter=$VCENTER_DATACENTER")
fi
[ -n "$VALIDATE_CERTS" ] && args+=(-e "validate_certs=$VALIDATE_CERTS")
args+=(-e "quiesce_snapshot=$QUIESCE_SNAPSHOT")
[ -n "$ARTIFACT_ROOT" ] && args+=(-e "artifact_root=$ARTIFACT_ROOT")

wrapper_run_playbook playbooks/migrate-aws-vm.yml "${args[@]}" "${wrapper_extra_args[@]}" -v
