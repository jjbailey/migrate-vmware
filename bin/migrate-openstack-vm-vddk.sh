#!/bin/bash
# bin/migrate-openstack-vm-vddk.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Datastore-direct migration to OpenStack: snapshots the running VM and streams
# its disks straight from the datastore, with no clone and no OVF export.
# See doc/datastore-direct.md. Takes the same environment variables as
# migrate-openstack-vm.sh, plus BACKEND (auto, vddk, or curl; default auto),
# COMPRESS (compress the QCOW2 for a slow upload link; default false),
# REQUIRE_CBT (default false), DISCARD_RUN_ID (remove a failed run's
# snapshot, run directory, and uploaded images instead of migrating), and
# VCENTER_CA_BUNDLE (PEM file of the CA that issued the vCenter certificate).

set -euo pipefail
wrapper_name=migrate-openstack-vm-vddk
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
OS_PROJECT_NAME=${OS_PROJECT_NAME:-}
ARTIFACT_ROOT=${ARTIFACT_ROOT:-/var/tmp/migrate-vmware}
REQUIRE_CBT=${REQUIRE_CBT:-}
BACKEND=${BACKEND:-}
COMPRESS=${COMPRESS:-}
DISCARD_RUN_ID=${DISCARD_RUN_ID:-}
VCENTER_CA_BUNDLE=${VCENTER_CA_BUNDLE:-}

wrapper_require_bool VALIDATE_CERTS "$VALIDATE_CERTS"
wrapper_require_bool QUIESCE_SNAPSHOT "$QUIESCE_SNAPSHOT"
wrapper_require_bool REQUIRE_CBT "$REQUIRE_CBT"
wrapper_require_bool COMPRESS "$COMPRESS"
case "$BACKEND" in
    "" | auto | vddk | curl) ;;
    *)
        echo "$wrapper_name: error: BACKEND must be auto, vddk, or curl (got '$BACKEND')" >&2
        exit 2
        ;;
esac

args=()
[ -n "$VM_NAME" ] && args+=(-e "$(wrapper_var vm_name "$VM_NAME")")
[ -n "$VSPHERE_TARGET_VCENTER" ] && args+=(-e "$(wrapper_var vsphere_target_vcenter "$VSPHERE_TARGET_VCENTER")")
if [ -z "$VSPHERE_TARGET_VCENTER" ] && [ -n "$VCENTER_DATACENTER" ] ; then
    args+=(-e "$(wrapper_var datacenter "$VCENTER_DATACENTER")")
fi
[ -n "$VALIDATE_CERTS" ] && args+=(-e "$(wrapper_var validate_certs "$VALIDATE_CERTS")")
args+=(-e "$(wrapper_var quiesce_snapshot "$QUIESCE_SNAPSHOT")")
[ -n "$OS_PROJECT_NAME" ] && args+=(-e "$(wrapper_var openstack_target_project "$OS_PROJECT_NAME")")
[ -n "$ARTIFACT_ROOT" ] && args+=(-e "$(wrapper_var artifact_root "$ARTIFACT_ROOT")")
[ -n "$REQUIRE_CBT" ] && args+=(-e "$(wrapper_var openstack_vddk_require_cbt "$REQUIRE_CBT")")
[ -n "$BACKEND" ] && args+=(-e "$(wrapper_var openstack_vddk_backend "$BACKEND")")
[ -n "$COMPRESS" ] && args+=(-e "$(wrapper_var openstack_convert_compress "$COMPRESS")")
[ -n "$DISCARD_RUN_ID" ] && args+=(-e "$(wrapper_var discard_run_id "$DISCARD_RUN_ID")")
[ -n "$VCENTER_CA_BUNDLE" ] && args+=(-e "$(wrapper_var openstack_vddk_ca_bundle "$VCENTER_CA_BUNDLE")")

wrapper_run_playbook playbooks/migrate-openstack-vm-vddk.yml "${args[@]}" "${wrapper_extra_args[@]}" -v
