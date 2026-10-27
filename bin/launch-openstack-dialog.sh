#!/bin/bash
# bin/launch-openstack-dialog.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Dialog front end for bin/launch-openstack-instance.sh. Nothing launches until
# the final confirmation.

script_dir=$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)
cd "$script_dir/.." || exit
# shellcheck disable=SC2034 # read by launch-dialog-common.sh
DIALOG_NAME=launch-openstack-dialog
# shellcheck source=launch-dialog-common.sh
source "$script_dir/launch-dialog-common.sh"

looks_like_ip()
{
    [[ $1 != *%* ]] && python3 -c 'import ipaddress, sys ; ipaddress.ip_address(sys.argv[1])' "$1" 2> /dev/null
}

looks_like_mac()
{
    [[ $1 =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]]
}

# Query one openstack-project-facts.py section and format tag\item\status
# lines for radio(). KIND selects the formatter (flavors|networks|keypairs) ;
# CURRENT pre-selects the existing value, or is kept as a
# "(current value, not in list)" row when absent. PROJECT may be empty for
# a single-entry vault (the facts script auto-selects) ; returns 1 when the
# lookup fails so the caller falls back to manual entry. stdin is /dev/null
# so a missing vault password fails fast instead of prompting in the dialog.
load_os_section_choices()
{
    local project=$1 section=$2 kind=$3 current=${4:-} facts args=()
    [ -x "$script_dir/openstack-project-facts.py" ] || return 1
    [ -n "$project" ] && args+=("$project")
    args+=("$section")
    if command -v timeout > /dev/null 2>&1 ; then
        facts=$(timeout 60 "$script_dir/openstack-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    else
        facts=$("$script_dir/openstack-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    fi
    [ -n "$facts" ] || return 1
    python3 -c '
import json, sys
facts = json.loads(sys.argv[1])
cur = sys.argv[2] if len(sys.argv) > 2 else ""
kind = sys.argv[3] if len(sys.argv) > 3 else ""
rows = facts.get({"flavors": "flavors", "networks": "networks",
                  "keypairs": "keypairs"}.get(kind, "")) or []
out = []
for row in rows:
    name = str(row.get("name") or "").strip()
    if not name:
        continue
    if kind == "flavors":
        label = "%s vCPU, %s MB, %s GB" % (
            row.get("vcpus") or "?", row.get("ram_mb") or "?",
            row.get("disk_gb") or "?")
        if row.get("fits") and row.get("fits") != "yes":
            label += " [%s]" % row.get("fits")
    elif kind == "networks":
        detail = ", ".join([str(part) for part in
                             (row.get("access"), row.get("status"))
                             if part])
        subs = str(row.get("subnets") or "").strip()
        label = detail + (" - " + subs if detail and subs else subs or detail)
    else:
        label = ", ".join([str(part) for part in
                            (row.get("type"), row.get("fingerprint"))
                            if part]) or "keypair"
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

# Single-select OpenStack picker with manual fallback. Prints the chosen
# value. _other is first so a known value can be typed without scrolling a
# long list ; a failed lookup goes straight to manual entry.
pick_os_value()
{
    local title=$1 project=$2 section=$3 kind=$4 current=$5 default=$6 prompt=$7
    local choices args=() tag label state
    [ -n "$current" ] || current=$default
    choices=$(load_os_section_choices "$project" "$section" "$kind" "$current") || choices=""
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

# Try to list the security groups PROJECT can use, for the multi-select
# picker. Prints tag\item\status lines (one group per line) for checklist()
# and returns 0, or returns 1 when the lookup fails so the caller falls back
# to manual entry. stdin is /dev/null so a missing vault password fails fast
# instead of prompting inside the dialog.
load_security_group_choices()
{
    local project=$1 facts args=()
    [ -x "$script_dir/openstack-project-facts.py" ] || return 1
    [ -n "$project" ] && args+=("$project")
    args+=(--security-groups)
    if command -v timeout > /dev/null 2>&1 ; then
        facts=$(timeout 60 "$script_dir/openstack-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    else
        facts=$("$script_dir/openstack-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    fi
    [ -n "$facts" ] || return 1
    python3 -c '
import json, sys
try:
    facts = json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
seen = set()
out = []
for group in facts.get("security_groups") or []:
    name = str(group.get("name") or "").strip()
    if not name or name in seen:
        continue
    seen.add(name)
    detail = "%s, %s" % (group.get("access") or "?", group.get("rules") or "?")
    desc = str(group.get("description") or "").splitlines()
    desc = desc[0][:60] if desc and desc[0].strip() else ""
    label = detail + (" - " + desc if desc else "")
    status = "ON" if name == "default" else "OFF"
    out.append("%s\t%s\t%s" % (name, label, status))
if not out:
    raise SystemExit(1)
print("\n".join(out))' "$facts" 2> /dev/null
}

# Project first: it decides where the instance and its volumes land.
PROJECT=$(choose_target "Which OpenStack project should this VM launch into?" vaults/openstack.yml openstack_projects "${OS_PROJECT_NAME:-}") || abort
VM_NAME=$(choose_vm openstack OpenStack) || abort
require_nonempty "$VM_NAME" "The VM name cannot be empty."

FIXED_IPS_JSON=""
addr_mode=$(radio "How should the new instance get its IP addresses?" \
    dhcp "DHCP - addresses assigned automatically (recommended)" ON \
    static "Static - specify fixed IPs, one per network interface" OFF) || abort
if [ "$addr_mode" = static ] ; then
    while : ; do
        ips=$(input "Fixed IP addresses, one per NIC, comma-separated:\n(IPv4 or IPv6 ; e.g. 10.0.0.5, or 10.0.0.5,2001:db8::5)" "") || abort
        ok=1
        IFS=',' read -r -a ip_list <<< "$ips"
        for ip in "${ip_list[@]}" ; do
            ip="${ip# }"
            ip="${ip% }"
            looks_like_ip "$ip" || {
                ok=0
                break
            }
        done
        [ "$ok" -eq 1 ] && [ -n "$ips" ] && {
            FIXED_IPS_JSON=$(json_list "$ips")
            break
        }
        bad "Each entry must be an IPv4 or IPv6 address (got: $ips)." || true
    done
fi

MAC_JSON=""
PRESERVE_MAC=false
mac_mode=$(radio "How should MAC addresses be handled?" \
    auto "Auto-assign - new MACs generated (recommended)" ON \
    preserve "Preserve source VMware MACs" OFF \
    manual "Specify MAC addresses manually" OFF) || abort
case "$mac_mode" in
    preserve) PRESERVE_MAC=true ;;
    manual)
        PRESERVE_MAC=true
        while : ; do
            macs=$(input "MAC addresses, one per NIC, comma-separated (aa:bb:cc:11:22:33):" "") || abort
            ok=1
            IFS=',' read -r -a mac_list <<< "$macs"
            for mac in "${mac_list[@]}" ; do
                mac="${mac# }"
                mac="${mac% }"
                looks_like_mac "$mac" || {
                    ok=0
                    break
                }
            done
            [ "$ok" -eq 1 ] && [ -n "$macs" ] && {
                MAC_JSON=$(json_list "$macs")
                break
            }
            bad "Each entry must be a MAC address like aa:bb:cc:11:22:33 (got: $macs)." || true
        done
        ;;
esac

# Static addressing and an automatic floating IP are mutually exclusive here.
if [ "$addr_mode" = static ] ; then
    AUTO_IP=false
elif yesno "Request a floating (public) IP for the instance?\n\nChoose Yes for a VM that must be reachable from outside." yes ; then
    AUTO_IP=true
else
    AUTO_IP=false
fi

SGS_JSON=""
SG_IDS_JSON=""
sg_mode=$(radio "Which security groups should the instance use?" \
    pick "Choose from this project's groups (recommended)" ON \
    names "Type group names manually" OFF \
    ids "By UUID" OFF \
    none "None (cloud default policy applies)" OFF) || abort
case "$sg_mode" in
    pick)
        sg_choices=$(load_security_group_choices "$PROJECT") || sg_choices=""
        if [ -z "$sg_choices" ] ; then
            say "Could not list security groups for '${PROJECT:-auto-select}'.\\n\\nEnter the names manually instead." 9 || true
            while : ; do
                sgs=$(input "Security group names, comma-separated:" "default") || abort
                [ -n "$sgs" ] && {
                    SGS_JSON=$(json_list "$sgs")
                    break
                }
                bad "Enter at least one security group name." || true
            done
        else
            sg_args=()
            while IFS=$'\t' read -r sg_tag sg_label sg_state ; do
                [ -n "$sg_tag" ] || continue
                sg_args+=("$sg_tag" "$sg_label" "$sg_state")
            done <<< "$sg_choices"
            while : ; do
                picked=$(checklist 'Which security groups should the instance join?\n(Space toggles, Enter confirms ; choose more than one when needed.)' "${sg_args[@]}") || abort
                picked=$(printf '%s' "$picked" | sed -e '/^[[:space:]]*$/d' | paste -sd ',' -)
                [ -n "$picked" ] && {
                    SGS_JSON=$(json_list "$picked")
                    break
                }
                if yesno 'No security groups selected.\n\nProceed with none (cloud default policy applies)?' no ; then
                    break
                fi
            done
        fi
        ;;
    names)
        while : ; do
            sgs=$(input "Security group names, comma-separated:" "default") || abort
            [ -n "$sgs" ] && {
                SGS_JSON=$(json_list "$sgs")
                break
            }
            bad "Enter at least one security group name." || true
        done
        ;;
    ids)
        while : ; do
            sgids=$(input "Security group UUIDs, comma-separated:" "") || abort
            [ -n "$sgids" ] && {
                SG_IDS_JSON=$(json_list "$sgids")
                break
            }
            bad "Enter at least one security group UUID." || true
        done
        ;;
esac

if yesno "Attach a config drive?\n\nSay Yes when the metadata service is unavailable. Otherwise No is fine." no ; then
    CONFIG_DRIVE=true
else
    CONFIG_DRIVE=false
fi

USER_DATA_FILE=""
DHCP_USER_DATA=false
ud_mode=$(radio "Should cloud-init user-data be applied on first boot?" \
    none "None - the guest image defaults apply" ON \
    dhcp "Use the bundled DHCP-only example user-data" OFF \
    file "Use a cloud-config file from this machine" OFF) || abort
case "$ud_mode" in
    dhcp) DHCP_USER_DATA=true ;;
    file)
        while : ; do
            USER_DATA_FILE=$(input "Path to the cloud-config file:" "") || abort
            [ -s "$USER_DATA_FILE" ] && {
                USER_DATA_FILE=$(realpath -- "$USER_DATA_FILE")
                break
            }
            bad "File not found or empty: $USER_DATA_FILE" || true
        done
        ;;
esac

while : ; do
    FLAVOR=$(pick_os_value "Which flavor (instance size) should the instance use?" "$PROJECT" "--flavors" "flavors" "${OS_FLAVOR:-${FLAVOR:-}}" "s1-4" "OpenStack flavor (instance size):") || abort
    KEY_NAME=$(pick_os_value "Which SSH key pair should the instance use?" "$PROJECT" "--keypairs" "keypairs" "${OS_KEY_NAME:-${KEY_NAME:-}}" "cloud" "SSH key pair name registered in OpenStack:") || abort
    NETWORK=$(pick_os_value "Which network should the instance attach to?" "$PROJECT" "--networks" "networks" "${OS_NETWORK:-${NETWORK:-}}" "Ext-Net" "OpenStack network name:") || abort
    [ -n "$FLAVOR" ] && [ -n "$KEY_NAME" ] && [ -n "$NETWORK" ] && break
    bad "Flavor, SSH key, and network cannot be empty." || true
done
NETWORK_ID=$(input "Network UUID (optional ; leave blank to use the name):" "") || abort

say "Launch summary for ${VM_NAME}

  Project....: ${PROJECT:-auto-select}
  Addressing.: ${addr_mode}${FIXED_IPS_JSON:+ ($FIXED_IPS_JSON)}
  MACs.......: ${mac_mode}${MAC_JSON:+ ($MAC_JSON)}
  Floating IP: ${AUTO_IP}
  Security...: ${sg_mode}${SGS_JSON:+ ($SGS_JSON)}${SG_IDS_JSON:+ ($SG_IDS_JSON)}
  ConfigDrive: ${CONFIG_DRIVE}
  User data..: ${ud_mode}${USER_DATA_FILE:+ ($USER_DATA_FILE)}
  Flavor.....: ${FLAVOR}
  SSH key....: ${KEY_NAME}
  Network....: ${NETWORK}${NETWORK_ID:+ ($NETWORK_ID)}" 18

file=$(mktemp /tmp/launch-openstack.XXXXXX)
{
    printf '#!/bin/bash\n'
    printf 'export VM_NAME=%s\n' "$(quote "$VM_NAME")"
    printf 'export OS_FLAVOR=%s\n' "$(quote "$FLAVOR")"
    printf 'export OS_KEY_NAME=%s\n' "$(quote "$KEY_NAME")"
    printf 'export OS_NETWORK=%s\n' "$(quote "$NETWORK")"
    [ -n "$SGS_JSON" ] && printf 'export OS_SECURITY_GROUPS_JSON=%s\n' "$(quote "$SGS_JSON")"
    [ -n "$SG_IDS_JSON" ] && printf 'export OS_SECURITY_GROUP_IDS_JSON=%s\n' "$(quote "$SG_IDS_JSON")"
    [ -n "$NETWORK_ID" ] && printf 'export OS_NETWORK_ID=%s\n' "$(quote "$NETWORK_ID")"
    printf 'export OS_PRESERVE_MAC=%s\n' "$(quote "$PRESERVE_MAC")"
    [ -n "$MAC_JSON" ] && printf 'export OS_MAC_ADDRESSES_JSON=%s\n' "$(quote "$MAC_JSON")"
    [ -n "$FIXED_IPS_JSON" ] && printf 'export OS_FIXED_IPS_JSON=%s\n' "$(quote "$FIXED_IPS_JSON")"
    printf 'export OS_AUTO_IP=%s\n' "$(quote "$AUTO_IP")"
    printf 'export OS_CONFIG_DRIVE=%s\n' "$(quote "$CONFIG_DRIVE")"
    printf 'export OS_DHCP_USER_DATA=%s\n' "$(quote "$DHCP_USER_DATA")"
    [ -n "$USER_DATA_FILE" ] && printf 'export OS_USER_DATA_FILE=%s\n' "$(quote "$USER_DATA_FILE")"
    [ -n "$PROJECT" ] && printf 'export OS_PROJECT_NAME=%s\n' "$(quote "$PROJECT")"
    printf '%s\n' "$(quote "$script_dir/launch-openstack-instance.sh")"
} > "$file"
chmod +x "$file"
finish "$file" "$VM_NAME"
