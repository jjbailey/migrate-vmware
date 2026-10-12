#!/bin/bash
# bin/launch-gcp-dialog.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Dialog front end for bin/launch-gcp-instance.sh. Nothing launches until the
# final confirmation.

script_dir=$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)
cd "$script_dir/.." || exit
# shellcheck disable=SC2034 # read by launch-dialog-common.sh
DIALOG_NAME=launch-gcp-dialog
# shellcheck source=launch-dialog-common.sh
source "$script_dir/launch-dialog-common.sh"

GCP_TARGET_PROJECT=$(choose_target "Which GCP project should this VM launch into?" vaults/gcp.yml gcp_projects "${GCP_TARGET_PROJECT:-}") || abort
VM_NAME=$(choose_vm gcp GCP) || abort
require_nonempty "$VM_NAME" "The VM name cannot be empty."

while : ; do
    GCP_ZONE=$(input "GCP zone:" "${GCP_ZONE:-us-central1-a}") || abort
    GCP_MACHINE_TYPE=$(input "Machine type:" "${GCP_MACHINE_TYPE:-e2-standard-2}") || abort
    GCP_NETWORK=$(input "Network:" "${GCP_NETWORK:-default}") || abort
    [ -n "$GCP_ZONE" ] && [ -n "$GCP_MACHINE_TYPE" ] && [ -n "$GCP_NETWORK" ] && break
    bad "Zone, machine type, and network cannot be empty." || true
done
GCP_SUBNETWORK=$(input "Subnetwork (optional):" "${GCP_SUBNETWORK:-}") || abort
tags=$(input "Network tags, comma-separated (optional):" "") || abort
GCP_TAGS_JSON=$(json_list "$tags")
if yesno "Launch without an external IP address?" no ; then GCP_NO_ADDRESS=true; else GCP_NO_ADDRESS=false; fi

GCP_SSH_KEY_FILE=""
GCP_SSH_USER=cloudinit
if yesno "Inject an additional SSH public key?\n\nThe project-level key is normally sufficient." no ; then
    while : ; do
        GCP_SSH_KEY_FILE=$(input "SSH public key file:" "") || abort
        [ -r "$GCP_SSH_KEY_FILE" ] && {
            GCP_SSH_KEY_FILE=$(realpath -- "$GCP_SSH_KEY_FILE")
            break
        }
        bad "That file is not readable: $GCP_SSH_KEY_FILE" || true
    done
    GCP_SSH_USER=$(input "SSH metadata user:" "cloudinit") || abort
    require_nonempty "$GCP_SSH_USER" "The SSH metadata user cannot be empty."
fi

say "Launch summary for $VM_NAME

  Project......: ${GCP_TARGET_PROJECT:-auto-select}
  Zone.........: $GCP_ZONE
  Machine type.: $GCP_MACHINE_TYPE
  Network......: $GCP_NETWORK
  Subnetwork...: ${GCP_SUBNETWORK:-default}
  Tags.........: $GCP_TAGS_JSON
  No address...: $GCP_NO_ADDRESS
  Extra SSH key: ${GCP_SSH_KEY_FILE:-none}" 16

file=$(mktemp /tmp/launch-gcp.XXXXXX)
{
    printf '#!/bin/bash\n'
    for pair in "VM_NAME=$VM_NAME" "GCP_ZONE=$GCP_ZONE" "GCP_MACHINE_TYPE=$GCP_MACHINE_TYPE" \
        "GCP_NETWORK=$GCP_NETWORK" "GCP_SUBNETWORK=$GCP_SUBNETWORK" "GCP_TAGS_JSON=$GCP_TAGS_JSON" \
        "GCP_NO_ADDRESS=$GCP_NO_ADDRESS" "GCP_TARGET_PROJECT=$GCP_TARGET_PROJECT" ; do
        printf 'export %s=%s\n' "${pair%%=*}" "$(quote "${pair#*=}")"
    done
    if [ -n "$GCP_SSH_KEY_FILE" ] ; then
        printf 'export GCP_SSH_USER=%s\n' "$(quote "$GCP_SSH_USER")"
        printf 'export GCP_SSH_KEY_FILE=%s\n' "$(quote "$GCP_SSH_KEY_FILE")"
    fi
    printf '%s\n' "$(quote "$script_dir/launch-gcp-instance.sh")"
} > "$file"
chmod +x "$file"
finish "$file" "$VM_NAME"
