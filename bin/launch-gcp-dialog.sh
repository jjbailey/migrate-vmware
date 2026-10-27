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

# Query one gcp-project-facts.py section and format tag\item\status lines
# for radio(). KIND selects the formatter
# (zones|machine_types|networks|subnetworks) ; CURRENT pre-selects the
# existing value, or is kept as a "(current value, not in list)" row when
# absent. EXTRA carries mode flags (--zone/--region). Returns 1 when the
# lookup fails so the caller falls back to manual entry.
load_gcp_section_choices()
{
    local project=$1 flag=$2 kind=$3 current=${4:-} facts args=()
    shift 4
    [ -x "$script_dir/gcp-project-facts.py" ] || return 1
    [ -n "$project" ] && args+=("$project")
    args+=("$flag" "$@")
    if command -v timeout > /dev/null 2>&1 ; then
        facts=$(timeout 60 "$script_dir/gcp-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    else
        facts=$("$script_dir/gcp-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    fi
    [ -n "$facts" ] || return 1
    python3 -c '
import json, sys
facts = json.loads(sys.argv[1])
cur = sys.argv[2] if len(sys.argv) > 2 else ""
kind = sys.argv[3] if len(sys.argv) > 3 else ""
key = {"zones": "zones", "machine_types": "machine_types",
       "networks": "networks", "subnetworks": "subnetworks"}.get(kind, "")
rows = facts.get(key) or []
out = []
for row in rows:
    name = str(row.get("name") or "").strip()
    if not name:
        continue
    if kind == "zones":
        label = str(row.get("region") or "")
    elif kind == "machine_types":
        label = "%s vCPU, %s MB" % (row.get("vcpus") or "?",
                                     row.get("memory_mb") or "?")
    elif kind == "networks":
        label = ", ".join([str(part) for part in
                            (row.get("mode"), row.get("routing"))
                            if part]) or "network"
    else:
        cidr = str(row.get("cidr") or "")
        net = str(row.get("network") or "")
        label = " - ".join([part for part in (cidr, net) if part]) or "subnet"
    if cur:
        status = "ON" if name == cur else "OFF"
    else:
        status = "ON" if not out else "OFF"
    out.append("%s\t%s\t%s" % (name, label, status))
if not out:
    raise SystemExit(1)
if cur and not any(row.split("\t")[0] == cur for row in out):
    out.append("%s\t%s\t%s" % (
        cur, cur + " (current value, not in list)", "ON"))
print("\n".join(out))' "$facts" "$current" "$kind" 2> /dev/null
}

# Single-select GCP picker with manual fallback. Prints the chosen value.
# _other is first so a known value can be typed without scrolling a long
# list ; a failed lookup goes straight to manual entry.
pick_gcp_value()
{
    local title=$1 project=$2 flag=$3 kind=$4 current=$5 default=$6 prompt=$7
    shift 7
    local choices args=() tag label state
    [ -n "$current" ] || current=$default
    choices=$(load_gcp_section_choices "$project" "$flag" "$kind" "$current" "$@") || choices=""
    if [ -z "$choices" ] ; then
        say "Could not list ${kind} for '${project:-auto-select}'.\\n\\nEnter the value manually instead." 9 || true
        input "$prompt" "$current" && return
        return 1
    fi
    args=(_other "Type manually (skip the list)" OFF)
    while IFS=$'\t' read -r tag label state ; do
        [ -n "$tag" ] || continue
        args+=("$tag" "$label" "$state")
    done <<< "$choices"
    tag=$(radio "$title\\n(Select Type manually to enter a known value.)" "${args[@]}") || return 1
    if [ "$tag" = _other ] ; then
        input "$prompt" "$current" && return
        return 1
    fi
    printf %s "$tag"
}

while : ; do
    GCP_ZONE=$(pick_gcp_value "Which zone should the instance launch into?" "$GCP_TARGET_PROJECT" "--zones" "zones" "${GCP_ZONE:-}" "us-central1-a" "GCP zone:") || abort
    GCP_MACHINE_TYPE=$(pick_gcp_value "Which machine type should the instance use?" "$GCP_TARGET_PROJECT" "--machine-types" "machine_types" "${GCP_MACHINE_TYPE:-}" "e2-standard-2" "Machine type:" --zone "$GCP_ZONE") || abort
    GCP_NETWORK=$(pick_gcp_value "Which network should the instance attach to?" "$GCP_TARGET_PROJECT" "--networks" "networks" "${GCP_NETWORK:-}" "default" "Network:") || abort
    [ -n "$GCP_ZONE" ] && [ -n "$GCP_MACHINE_TYPE" ] && [ -n "$GCP_NETWORK" ] && break
    bad "Zone, machine type, and network cannot be empty." || true
done
while : ; do
    sub_choices=$(load_gcp_section_choices "$GCP_TARGET_PROJECT" "--subnetworks" "subnetworks" "${GCP_SUBNETWORK:-}" --zone "$GCP_ZONE") || sub_choices=""
    if [ -z "$sub_choices" ] ; then
        GCP_SUBNETWORK=$(input "Subnetwork (optional):" "${GCP_SUBNETWORK:-}") || abort
        break
    fi
    sub_args=(_other "Type a subnetwork manually (skip the list)" OFF)
    while IFS=$'\t' read -r sub_tag sub_label sub_state ; do
        [ -n "$sub_tag" ] || continue
        sub_args+=("$sub_tag" "$sub_label" "$sub_state")
    done <<< "$sub_choices"
    sub_args+=(_none "No subnetwork (network default)" OFF)
    picked_sub=$(radio 'Which subnetwork should the instance use? (optional)\n(Select Type manually to enter a known value.)' "${sub_args[@]}") || abort
    if [ "$picked_sub" = _none ] ; then
        GCP_SUBNETWORK=""
        break
    elif [ "$picked_sub" = _other ] ; then
        GCP_SUBNETWORK=$(input "Subnetwork (optional):" "${GCP_SUBNETWORK:-}") || abort
        break
    else
        GCP_SUBNETWORK=$picked_sub
        break
    fi
done
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
