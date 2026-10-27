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

# Try to list the subnets for PROJECT, for the single-select picker.
# Prints tag\item\status lines (tag is the subnet-... ID) for radio() and
# returns 0, or returns 1 when the lookup fails so the caller falls back to
# manual entry. CURRENT, when given, is pre-selected instead of the first
# row. stdin is /dev/null so a missing vault password fails fast instead
# of prompting inside the dialog.
load_aws_subnet_choices()
{
    local project=$1 current=${2:-} facts args=()
    [ -x "$script_dir/aws-project-facts.py" ] || return 1
    [ -n "$project" ] && args+=("$project")
    args+=(--subnets)
    if command -v timeout > /dev/null 2>&1 ; then
        facts=$(timeout 60 "$script_dir/aws-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    else
        facts=$("$script_dir/aws-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    fi
    [ -n "$facts" ] || return 1
    python3 -c '
import json, sys
try:
    facts = json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
current = sys.argv[2] if len(sys.argv) > 2 else ""
out = []
for item in facts.get("subnets") or []:
    sid = str(item.get("id") or "").strip()
    if not sid:
        continue
    state = str(item.get("state") or "").strip()
    if state and state != "available":
        continue
    name = str(item.get("name") or "").strip()
    cidr = str(item.get("cidr") or "").strip()
    az = str(item.get("az") or "").strip()
    vpc = str(item.get("vpc_id") or "").strip()
    try:
        free = int(item.get("available_ips") or 0)
    except (TypeError, ValueError):
        free = 0
    head = name or cidr or sid
    detail = ", ".join([part for part in (cidr, az) if part])
    label = head + (" (%s)" % detail if detail else "")
    tail = " ".join([part for part in (
        ("[%s]" % vpc if vpc else ""),
        ("%d free" % free),
        ("default" if item.get("default") else "")) if part])
    if tail:
        label += " " + tail
    if current:
        status = "ON" if sid == current else "OFF"
    else:
        status = "ON" if not out else "OFF"
    out.append("%s\t%s\t%s" % (sid, label, status))
if not out:
    raise SystemExit(1)
if current and not any(row.split("\t")[0] == current for row in out):
    out.append("%s\t%s\t%s" % (current, current + " (current value, not in list)", "ON"))
print("\n".join(out))' "$facts" "$current" 2> /dev/null
}

# Try to list the EC2 security groups for PROJECT, for the multi-select
# picker. Prints tag\item\status lines (tag is the sg-... ID) for
# checklist() and returns 0, or returns 1 when the lookup fails so the
# caller falls back to manual entry. SUBNET, when given, narrows the list
# to its VPC since a launch's groups must live in the subnet's VPC.
# stdin is /dev/null so a missing vault password fails fast instead of
# prompting inside the dialog.
load_aws_security_group_choices()
{
    local project=$1 subnet=${2:-} facts args=()
    [ -x "$script_dir/aws-project-facts.py" ] || return 1
    [ -n "$project" ] && args+=("$project")
    [ -n "$subnet" ] && args+=(--subnet "$subnet")
    if command -v timeout > /dev/null 2>&1 ; then
        facts=$(timeout 60 "$script_dir/aws-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    else
        facts=$("$script_dir/aws-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    fi
    [ -n "$facts" ] || return 1
    python3 -c '
import json, sys
try:
    facts = json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
out = []
for group in facts.get("security_groups") or []:
    gid = str(group.get("id") or "").strip()
    name = str(group.get("name") or "").strip()
    if not gid:
        continue
    detail = "%s, %s" % (name or "?", group.get("rules") or "?")
    desc = str(group.get("description") or "").splitlines()
    desc = desc[0][:60] if desc and desc[0].strip() else ""
    label = detail + (" - " + desc if desc else "")
    vpc = str(group.get("vpc_id") or "").strip()
    if vpc:
        label += " [%s]" % vpc
    status = "ON" if name == "default" else "OFF"
    out.append("%s\t%s\t%s" % (gid, label, status))
if not out:
    raise SystemExit(1)
print("\n".join(out))' "$facts" 2> /dev/null
}

# Try to list the EC2 keypairs for PROJECT, for the single-select picker.
# Prints tag\item\status lines for radio(). CURRENT pre-selects the
# existing value. Returns 1 when the lookup fails so the caller falls back
# to manual entry.
load_aws_keypair_choices()
{
    local project=$1 current=${2:-} facts args=()
    [ -x "$script_dir/aws-project-facts.py" ] || return 1
    [ -n "$project" ] && args+=("$project")
    args+=(--keypairs)
    if command -v timeout > /dev/null 2>&1 ; then
        facts=$(timeout 60 "$script_dir/aws-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    else
        facts=$("$script_dir/aws-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    fi
    [ -n "$facts" ] || return 1
    python3 -c '
import json, sys
try:
    facts = json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
cur = sys.argv[2] if len(sys.argv) > 2 else ""
out = []
for pair in facts.get("keypairs") or []:
    name = str(pair.get("name") or "").strip()
    if not name:
        continue
    detail = ", ".join([str(part) for part in
                         (pair.get("type"), pair.get("fingerprint"))
                         if part]) or "keypair"
    if cur:
        status = "ON" if name == cur else "OFF"
    else:
        status = "ON" if name == "cloud" else "OFF"
    out.append("%s\t%s\t%s" % (name, detail, status))
if not out:
    raise SystemExit(1)
if cur and not any(row.split("\t")[0] == cur for row in out):
    out.append("%s\t%s\t%s" % (cur, cur + " (current value, not in list)", "ON"))
print("\n".join(out))' "$facts" "$current" 2> /dev/null
}

# Curated EC2 instance types: the full list is hundreds long, so offer the
# common families and leave a manual row. CURRENT pre-selects the value.
# Always succeeds; the caller treats empty output as "no choices".
load_aws_instance_type_choices()
{
    local current=${1:-t3.medium} found=0 cand
    [ -n "$current" ] || current=t3.medium
    for cand in t3.micro t3.small t3.medium t3.large t3.xlarge \
        t3a.medium t3a.large m5.large m5.xlarge m5.2xlarge \
        m6i.large m6i.xlarge c5.large c5.xlarge c6i.large \
        r5.large r5.xlarge ; do
        if [ "$cand" = "$current" ] ; then
            found=1
            printf '%s\t%s\t%s\n' "$cand" "common instance type" ON
        else
            printf '%s\t%s\t%s\n' "$cand" "common instance type" OFF
        fi
    done
    if [ "$found" -eq 0 ] ; then
        printf '%s\t%s\t%s\n' "$current" "$current (current value)" ON
    fi
    return 0
}

AWS_TARGET_PROJECT=$(choose_target "Which AWS account should this VM launch into?" vaults/aws.yml aws_projects "${AWS_TARGET_PROJECT:-}") || abort
VM_NAME=$(choose_vm aws AWS) || abort
require_nonempty "$VM_NAME" "The VM name cannot be empty."

while : ; do
    subnet_choices=$(load_aws_subnet_choices "$AWS_TARGET_PROJECT" "${AWS_SUBNET_ID:-}") || subnet_choices=""
    if [ -z "$subnet_choices" ] ; then
        say "Could not list subnets for '${AWS_TARGET_PROJECT:-auto-select}'.\\n\\nEnter the subnet ID manually instead." 9 || true
        AWS_SUBNET_ID=$(input "AWS subnet ID (required):" "${AWS_SUBNET_ID:-}") || abort
        [ -n "$AWS_SUBNET_ID" ] && break
        bad "The subnet ID cannot be empty." || true
    else
        subnet_args=(_other "Type a subnet ID manually (skip the list)" OFF)
        while IFS=$'\t' read -r sub_tag sub_label sub_state ; do
            [ -n "$sub_tag" ] || continue
            subnet_args+=("$sub_tag" "$sub_label" "$sub_state")
        done <<< "$subnet_choices"
        picked_subnet=$(radio 'Which subnet should the instance launch into?\n(Select Type manually to enter a known ID.)' "${subnet_args[@]}") || abort
        if [ "$picked_subnet" = _other ] ; then
            AWS_SUBNET_ID=$(input "AWS subnet ID (required):" "${AWS_SUBNET_ID:-}") || abort
            [ -n "$AWS_SUBNET_ID" ] && break
            bad "The subnet ID cannot be empty." || true
        else
            AWS_SUBNET_ID=$picked_subnet
            break
        fi
    fi
done
AWS_SECURITY_GROUPS_JSON=""
sg_mode=$(radio "Which security groups should the instance use?" \
    pick "Choose from this account's groups (recommended)" ON \
    names "Type group names or IDs manually" OFF \
    none "None (VPC default applies)" OFF) || abort
case "$sg_mode" in
    pick)
        sg_choices=$(load_aws_security_group_choices "$AWS_TARGET_PROJECT" "$AWS_SUBNET_ID") || sg_choices=""
        if [ -z "$sg_choices" ] ; then
            say "Could not list security groups for '${AWS_TARGET_PROJECT:-auto-select}'.\\n\\nEnter them manually instead." 9 || true
            while : ; do
                groups=$(input "Security group names or IDs, comma-separated:" "default") || abort
                AWS_SECURITY_GROUPS_JSON=$(json_list "$groups")
                [ "$AWS_SECURITY_GROUPS_JSON" != '[]' ] && break
                bad "Enter at least one security group." || true
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
                    AWS_SECURITY_GROUPS_JSON=$(json_list "$picked")
                    break
                }
                if yesno 'No security groups selected.\n\nProceed with none (VPC default applies)?' no ; then
                    AWS_SECURITY_GROUPS_JSON='[]'
                    break
                fi
            done
        fi
        ;;
    names)
        while : ; do
            groups=$(input "Security group names or IDs, comma-separated:" "default") || abort
            AWS_SECURITY_GROUPS_JSON=$(json_list "$groups")
            [ "$AWS_SECURITY_GROUPS_JSON" != '[]' ] && break
            bad "Enter at least one security group." || true
        done
        ;;
    none)
        AWS_SECURITY_GROUPS_JSON='[]'
        ;;
esac
while : ; do
    type_choices=$(load_aws_instance_type_choices "${AWS_INSTANCE_TYPE:-t3.medium}") || type_choices=""
    type_args=(_other "Type a different instance type manually (skip the list)" OFF)
    while IFS=$'\t' read -r type_tag type_label type_state ; do
        [ -n "$type_tag" ] || continue
        type_args+=("$type_tag" "$type_label" "$type_state")
    done <<< "$type_choices"
    type_args+=(_blank "Leave blank for the playbook default" OFF)
    picked_type=$(radio 'Which instance type should the instance use?\n(Select Type manually to enter a known value.)' "${type_args[@]}") || abort
    if [ "$picked_type" = _blank ] ; then
        AWS_INSTANCE_TYPE=""
        break
    elif [ "$picked_type" = _other ] ; then
        AWS_INSTANCE_TYPE=$(input "Instance type (blank for the playbook default):" "${AWS_INSTANCE_TYPE:-}") || abort
        break
    else
        AWS_INSTANCE_TYPE=$picked_type
        break
    fi
done
while : ; do
    key_choices=$(load_aws_keypair_choices "$AWS_TARGET_PROJECT" "${AWS_KEY_NAME:-cloud}") || key_choices=""
    if [ -z "$key_choices" ] ; then
        say "Could not list keypairs for '${AWS_TARGET_PROJECT:-auto-select}'.\\n\\nEnter the key name manually instead." 9 || true
        AWS_KEY_NAME=$(input "EC2 key pair name (blank for none):" "${AWS_KEY_NAME:-cloud}") || abort
        break
    fi
    key_args=(_other "Type a different key name manually (skip the list)" OFF)
    while IFS=$'\t' read -r key_tag key_label key_state ; do
        [ -n "$key_tag" ] || continue
        key_args+=("$key_tag" "$key_label" "$key_state")
    done <<< "$key_choices"
    key_args+=(_none "No key pair" OFF)
    picked_key=$(radio 'Which EC2 key pair should the instance use?\n(Select Type manually to enter a known value.)' "${key_args[@]}") || abort
    if [ "$picked_key" = _none ] ; then
        AWS_KEY_NAME=""
        break
    elif [ "$picked_key" = _other ] ; then
        AWS_KEY_NAME=$(input "EC2 key pair name (blank for none):" "${AWS_KEY_NAME:-cloud}") || abort
        break
    else
        AWS_KEY_NAME=$picked_key
        break
    fi
done

say "Launch summary for $VM_NAME

  Account........: ${AWS_TARGET_PROJECT:-auto-select}
  Subnet.........: $AWS_SUBNET_ID
  Instance type..: ${AWS_INSTANCE_TYPE:-playbook default}
  Security groups: ${sg_mode}${AWS_SECURITY_GROUPS_JSON:+ ($AWS_SECURITY_GROUPS_JSON)}
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
