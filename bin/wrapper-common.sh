#!/bin/bash
# bin/wrapper-common.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

set -euo pipefail

wrapper_source=${BASH_SOURCE[1]:-${BASH_SOURCE[0]}}
wrapper_script_dir=$(CDPATH='' builtin cd -- "$(dirname -- "$wrapper_source")" > /dev/null && builtin pwd -P)

wrapper_error()
{
    printf '%s: error: %s\n' "${wrapper_name:-wrapper}" "$1" >&2
    exit 2
}

wrapper_require_bool()
{
    local name=$1
    local value=$2
    case "$value" in
        true | false | "") ;;
        *) wrapper_error "$name must be true or false (got '$value')" ;;
    esac
}

wrapper_require_json_array()
{
    local name=$1
    local value=$2
    [ -n "$value" ] || return 0
    python3 -c '
import json
import sys

name, value = sys.argv[1:]
try:
    parsed = json.loads(value)
except json.JSONDecodeError as exc:
    raise SystemExit(f"{name} must be a JSON array: {exc}")
if not isinstance(parsed, list):
    raise SystemExit(f"{name} must be a JSON array")
' "$name" "$value" || wrapper_error "invalid $name"
}

# Ansible splits -e "key=value" on whitespace, so "Web Server 01" would arrive
# as vm_name=Web. These print a one-key JSON object for -e instead, which keeps
# any value intact: wrapper_var as a string, wrapper_json_var as the parsed
# JSON (a list stays a list). Use them as: args+=(-e "$(wrapper_var k "$v")")
wrapper_var()
{
    python3 -c '
import json
import sys

print(json.dumps({sys.argv[1]: sys.argv[2]}))
' "$1" "$2"
}

wrapper_json_var()
{
    python3 -c '
import json
import sys

print(json.dumps({sys.argv[1]: json.loads(sys.argv[2])}))
' "$1" "$2"
}

wrapper_capture_args()
{
    # shellcheck disable=SC2034
    wrapper_extra_args=("$@")
}

wrapper_run_playbook()
{
    exec "$wrapper_script_dir/run-playbook.sh" "$@"
}
