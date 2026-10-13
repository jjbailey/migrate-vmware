#!/bin/bash
# bin/launch-aws-dialog.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Dialog front end for bin/launch-aws-instance.sh. Nothing launches until the
# final confirmation.

script_dir=$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)
cd "$script_dir/.." || exit
# shellcheck disable=SC2034 # read by launch-dialog-common.sh
DIALOG_NAME=launch-aws-dialog
# shellcheck source=launch-dialog-common.sh
source "$script_dir/launch-dialog-common.sh"

AWS_TARGET_PROJECT=$(choose_target "Which AWS account should this VM launch into?" vaults/aws.yml aws_projects "${AWS_TARGET_PROJECT:-}") || abort
VM_NAME=$(choose_vm aws AWS) || abort
require_nonempty "$VM_NAME" "The VM name cannot be empty."

while : ; do
    AWS_SUBNET_ID=$(input "AWS subnet ID (required):" "${AWS_SUBNET_ID:-}") || abort
    [ -n "$AWS_SUBNET_ID" ] && break
    bad "The subnet ID cannot be empty." || true
done
while : ; do
    groups=$(input "Security group names or IDs, comma-separated:" "default") || abort
    AWS_SECURITY_GROUPS_JSON=$(json_list "$groups")
    [ "$AWS_SECURITY_GROUPS_JSON" != '[]' ] && break
    bad "Enter at least one security group." || true
done
AWS_INSTANCE_TYPE=$(input "Instance type (blank for the playbook default):" "${AWS_INSTANCE_TYPE:-}") || abort
AWS_KEY_NAME=$(input "EC2 key pair name (blank for none):" "${AWS_KEY_NAME:-cloud}") || abort

say "Launch summary for $VM_NAME

  Account........: ${AWS_TARGET_PROJECT:-auto-select}
  Subnet.........: $AWS_SUBNET_ID
  Instance type..: ${AWS_INSTANCE_TYPE:-playbook default}
  Security groups: $AWS_SECURITY_GROUPS_JSON
  SSH key pair...: ${AWS_KEY_NAME:-none}" 14

file=$(mktemp /tmp/launch-aws.XXXXXX)
{
    printf '#!/bin/bash\n'
    printf 'export AWS_TARGET_PROJECT=%s\n' "$(quote "$AWS_TARGET_PROJECT")"
    printf 'export VM_NAME=%s\n' "$(quote "$VM_NAME")"
    printf 'export AWS_SUBNET_ID=%s\n' "$(quote "$AWS_SUBNET_ID")"
    printf 'export AWS_SECURITY_GROUPS_JSON=%s\n' "$(quote "$AWS_SECURITY_GROUPS_JSON")"
    printf 'export AWS_INSTANCE_TYPE=%s\n' "$(quote "$AWS_INSTANCE_TYPE")"
    printf 'export AWS_KEY_NAME=%s\n' "$(quote "$AWS_KEY_NAME")"
    printf '%s\n' "$(quote "$script_dir/launch-aws-instance.sh")"
} > "$file"
chmod +x "$file"
finish "$file" "$VM_NAME"
