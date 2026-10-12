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
    names "By name (recommended ; e.g. default)" ON \
    ids "By UUID" OFF \
    none "None (cloud default policy applies)" OFF) || abort
case "$sg_mode" in
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
    FLAVOR=$(input "OpenStack flavor (instance size):" "s1-8") || abort
    KEY_NAME=$(input "SSH key pair name registered in OpenStack:" "cloud") || abort
    NETWORK=$(input "OpenStack network name:" "Ext-Net") || abort
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
