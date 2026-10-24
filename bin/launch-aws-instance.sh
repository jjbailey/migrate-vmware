#!/bin/bash
# bin/launch-aws-instance.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

set -euo pipefail
wrapper_name=launch-aws-instance
export wrapper_name
wrapper_extra_args=()
# shellcheck source=wrapper-common.sh
# shellcheck disable=SC1091
source "$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)/wrapper-common.sh"
wrapper_capture_args "$@"

VM_NAME=${VM_NAME:-}
AWS_TARGET_PROJECT=${AWS_TARGET_PROJECT:-}
AWS_SUBNET_ID=${AWS_SUBNET_ID:-}
AWS_SECURITY_GROUPS_JSON=${AWS_SECURITY_GROUPS_JSON:-'["default"]'}
AWS_KEY_NAME=${AWS_KEY_NAME:-cloud}
AWS_INSTANCE_TYPE=${AWS_INSTANCE_TYPE:-}

wrapper_require_json_array AWS_SECURITY_GROUPS_JSON "$AWS_SECURITY_GROUPS_JSON"

args=()
[ -n "$VM_NAME" ] && args+=(-e "$(wrapper_var vm_name "$VM_NAME")")
[ -n "$AWS_TARGET_PROJECT" ] && args+=(-e "$(wrapper_var aws_target_project "$AWS_TARGET_PROJECT")")
[ -n "$AWS_SUBNET_ID" ] && args+=(-e "$(wrapper_var vpc_subnet_id "$AWS_SUBNET_ID")")
[ -n "$AWS_SECURITY_GROUPS_JSON" ] && args+=(-e "$(wrapper_json_var security_groups "$AWS_SECURITY_GROUPS_JSON")")
[ -n "$AWS_KEY_NAME" ] && args+=(-e "$(wrapper_var key_name "$AWS_KEY_NAME")")
[ -n "$AWS_INSTANCE_TYPE" ] && args+=(-e "$(wrapper_var instance_type "$AWS_INSTANCE_TYPE")")

wrapper_run_playbook playbooks/launch-aws-instance.yml "${args[@]}" "${wrapper_extra_args[@]}" -v
