#!/bin/bash
# bin/launch-gcp-instance.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

set -euo pipefail
wrapper_name=launch-gcp-instance
export wrapper_name
wrapper_extra_args=()
# shellcheck source=wrapper-common.sh
# shellcheck disable=SC1091
source "$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)/wrapper-common.sh"
wrapper_capture_args "$@"

VM_NAME=${VM_NAME:-}
GCP_ZONE=${GCP_ZONE:-}
GCP_TARGET_PROJECT=${GCP_TARGET_PROJECT:-}
GCP_MACHINE_TYPE=${GCP_MACHINE_TYPE:-}
GCP_NETWORK=${GCP_NETWORK:-default}
GCP_SUBNETWORK=${GCP_SUBNETWORK:-}
GCP_TAGS_JSON=${GCP_TAGS_JSON:-[]}
GCP_NO_ADDRESS=${GCP_NO_ADDRESS:-false}
# GCP has no keypair resource. The equivalent of a pre-registered AWS or
# OpenStack keypair is a 'cloudinit:' entry in the project's ssh-keys metadata,
# which every instance inherits, so no per-launch key argument is needed.
# GCP_SSH_KEY_FILE injects an additional key as instance metadata for one
# launch; it adds to the project keys rather than replacing them.
GCP_SSH_USER=${GCP_SSH_USER:-cloudinit}
GCP_SSH_KEY_FILE=${GCP_SSH_KEY_FILE:-}

wrapper_require_bool GCP_NO_ADDRESS "$GCP_NO_ADDRESS"
wrapper_require_json_array GCP_TAGS_JSON "$GCP_TAGS_JSON"

args=()
[ -n "$VM_NAME" ] && args+=(-e "$(wrapper_var vm_name "$VM_NAME")")
[ -n "$GCP_ZONE" ] && args+=(-e "$(wrapper_var gcp_zone "$GCP_ZONE")")
[ -n "$GCP_TARGET_PROJECT" ] && args+=(-e "$(wrapper_var gcp_target_project "$GCP_TARGET_PROJECT")")
[ -n "$GCP_MACHINE_TYPE" ] && args+=(-e "$(wrapper_var gcp_machine_type "$GCP_MACHINE_TYPE")")
[ -n "$GCP_NETWORK" ] && args+=(-e "$(wrapper_var gcp_network "$GCP_NETWORK")")
[ -n "$GCP_SUBNETWORK" ] && args+=(-e "$(wrapper_var gcp_subnetwork "$GCP_SUBNETWORK")")
[ "$GCP_NO_ADDRESS" = true ] && args+=(-e "$(wrapper_var gcp_no_address "true")")
[ "$GCP_TAGS_JSON" != '[]' ] && args+=(-e "$(wrapper_json_var gcp_tags "$GCP_TAGS_JSON")")
if [ -n "$GCP_SSH_KEY_FILE" ] ; then
    if [ ! -r "$GCP_SSH_KEY_FILE" ] ; then
        echo "${wrapper_name}: GCP_SSH_KEY_FILE is not readable: $GCP_SSH_KEY_FILE" >&2
        exit 1
    fi
    args+=(-e "$(wrapper_var gcp_ssh_user "$GCP_SSH_USER")")
    args+=(-e "$(wrapper_var gcp_ssh_public_key_file "$GCP_SSH_KEY_FILE")")
fi

wrapper_run_playbook playbooks/launch-gcp-instance.yml "${args[@]}" "${wrapper_extra_args[@]}" -v
