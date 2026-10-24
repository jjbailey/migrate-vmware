#!/bin/bash
# bin/migrate-gcp-vm.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

set -euo pipefail
wrapper_name=migrate-gcp-vm
export wrapper_name
wrapper_extra_args=()
# shellcheck source=wrapper-common.sh
# shellcheck disable=SC1091
source "$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)/wrapper-common.sh"
wrapper_capture_args "$@"

VSPHERE_TARGET_VCENTER=${VSPHERE_TARGET_VCENTER:-}
VCENTER_DATACENTER=${VCENTER_DATACENTER:-}
VM_NAME=${VM_NAME:-}
VALIDATE_CERTS=${VALIDATE_CERTS:-}
QUIESCE_SNAPSHOT=${QUIESCE_SNAPSHOT:-true}
GCP_TARGET_PROJECT=${GCP_TARGET_PROJECT:-}
ARTIFACT_ROOT=${ARTIFACT_ROOT:-/var/tmp/migrate-vmware}

wrapper_require_bool VALIDATE_CERTS "$VALIDATE_CERTS"
wrapper_require_bool QUIESCE_SNAPSHOT "$QUIESCE_SNAPSHOT"

args=()
[ -n "$VM_NAME" ] && args+=(-e "$(wrapper_var vm_name "$VM_NAME")")
[ -n "$VSPHERE_TARGET_VCENTER" ] && args+=(-e "$(wrapper_var vsphere_target_vcenter "$VSPHERE_TARGET_VCENTER")")
if [ -z "$VSPHERE_TARGET_VCENTER" ] && [ -n "$VCENTER_DATACENTER" ] ; then
    args+=(-e "$(wrapper_var datacenter "$VCENTER_DATACENTER")")
fi
[ -n "$VALIDATE_CERTS" ] && args+=(-e "$(wrapper_var validate_certs "$VALIDATE_CERTS")")
[ -n "$GCP_TARGET_PROJECT" ] && args+=(-e "$(wrapper_var gcp_target_project "$GCP_TARGET_PROJECT")")
args+=(-e "$(wrapper_var quiesce_snapshot "$QUIESCE_SNAPSHOT")")
[ -n "$ARTIFACT_ROOT" ] && args+=(-e "$(wrapper_var artifact_root "$ARTIFACT_ROOT")")

wrapper_run_playbook playbooks/migrate-gcp-vm.yml "${args[@]}" "${wrapper_extra_args[@]}" -v
