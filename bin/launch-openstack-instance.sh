#!/bin/bash
# bin/launch-openstack-instance.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

set -euo pipefail
wrapper_name=launch-openstack-instance
export wrapper_name
wrapper_extra_args=()
# shellcheck source=wrapper-common.sh
# shellcheck disable=SC1091
source "$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)/wrapper-common.sh"
wrapper_capture_args "$@"

VM_NAME=${VM_NAME:-}
OS_FLAVOR=${OS_FLAVOR:-}
OS_KEY_NAME=${OS_KEY_NAME:-cloud}
OS_SECURITY_GROUPS_JSON=${OS_SECURITY_GROUPS_JSON:-'["default"]'}
OS_NETWORK=${OS_NETWORK:-}
OS_NETWORK_ID=${OS_NETWORK_ID:-}
OS_NICS_JSON=${OS_NICS_JSON:-}
OS_SECURITY_GROUP_IDS_JSON=${OS_SECURITY_GROUP_IDS_JSON:-}
OS_PRESERVE_MAC=${OS_PRESERVE_MAC:-false}
OS_MAC_ADDRESSES_JSON=${OS_MAC_ADDRESSES_JSON:-}
OS_FIXED_IPS_JSON=${OS_FIXED_IPS_JSON:-}
OS_AUTO_IP=${OS_AUTO_IP:-true}
OS_USER_DATA=${OS_USER_DATA:-}
OS_USER_DATA_FILE=${OS_USER_DATA_FILE:-}
OS_DHCP_USER_DATA=${OS_DHCP_USER_DATA:-false}
OS_CONFIG_DRIVE=${OS_CONFIG_DRIVE:-false}
OS_PROJECT_NAME=${OS_PROJECT_NAME:-}

wrapper_require_bool OS_PRESERVE_MAC "$OS_PRESERVE_MAC"
wrapper_require_bool OS_AUTO_IP "$OS_AUTO_IP"
wrapper_require_bool OS_DHCP_USER_DATA "$OS_DHCP_USER_DATA"
wrapper_require_bool OS_CONFIG_DRIVE "$OS_CONFIG_DRIVE"
wrapper_require_json_array OS_SECURITY_GROUPS_JSON "$OS_SECURITY_GROUPS_JSON"
wrapper_require_json_array OS_SECURITY_GROUP_IDS_JSON "$OS_SECURITY_GROUP_IDS_JSON"
wrapper_require_json_array OS_MAC_ADDRESSES_JSON "$OS_MAC_ADDRESSES_JSON"
wrapper_require_json_array OS_FIXED_IPS_JSON "$OS_FIXED_IPS_JSON"
wrapper_require_json_array OS_NICS_JSON "$OS_NICS_JSON"

args=()
[ -n "$VM_NAME" ] && args+=(-e "$(wrapper_var vm_name "$VM_NAME")")
[ -n "$OS_FLAVOR" ] && args+=(-e "$(wrapper_var openstack_flavor "$OS_FLAVOR")")
[ -n "$OS_NICS_JSON" ] && args+=(-e "$(wrapper_var openstack_network "")")
[ -z "$OS_NICS_JSON" ] && [ -n "$OS_NETWORK" ] && args+=(-e "$(wrapper_var openstack_network "$OS_NETWORK")")
[ -n "$OS_NETWORK_ID" ] && args+=(-e "$(wrapper_var openstack_network_id "$OS_NETWORK_ID")")
args+=(-e "$(wrapper_var openstack_preserve_mac "$OS_PRESERVE_MAC")")
args+=(-e "$(wrapper_var openstack_auto_ip "$OS_AUTO_IP")")
[ -n "$OS_KEY_NAME" ] && args+=(-e "$(wrapper_var openstack_key_name "$OS_KEY_NAME")")
args+=(-e "$(wrapper_var openstack_dhcp_user_data "$OS_DHCP_USER_DATA")")
args+=(-e "$(wrapper_var openstack_config_drive "$OS_CONFIG_DRIVE")")

if [ -n "$OS_SECURITY_GROUP_IDS_JSON" ] ; then
    args+=(-e "$(wrapper_json_var openstack_security_groups '[]')")
    args+=(-e "$(wrapper_json_var openstack_security_group_ids "$OS_SECURITY_GROUP_IDS_JSON")")
else
    args+=(-e "$(wrapper_json_var openstack_security_groups "$OS_SECURITY_GROUPS_JSON")")
fi

[ -n "$OS_MAC_ADDRESSES_JSON" ] && args+=(-e "$(wrapper_json_var openstack_mac_addresses "$OS_MAC_ADDRESSES_JSON")")
[ -n "$OS_FIXED_IPS_JSON" ] && args+=(-e "$(wrapper_json_var openstack_fixed_ips "$OS_FIXED_IPS_JSON")")
[ -n "$OS_NICS_JSON" ] && args+=(-e "$(wrapper_json_var openstack_nics "$OS_NICS_JSON")")
[ -n "$OS_PROJECT_NAME" ] && args+=(-e "$(wrapper_var openstack_target_project "$OS_PROJECT_NAME")")
[ -n "$OS_USER_DATA" ] && args+=(-e "$(wrapper_var openstack_user_data "$OS_USER_DATA")")
[ -n "$OS_USER_DATA_FILE" ] && args+=(-e "$(wrapper_var openstack_user_data_file "$OS_USER_DATA_FILE")")

wrapper_run_playbook playbooks/launch-openstack-instance.yml "${args[@]}" "${wrapper_extra_args[@]}" -v
