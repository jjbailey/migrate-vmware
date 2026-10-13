# Playbook usage examples

<!-- markdownlint-disable MD013 -->

Copy-paste oriented examples for the migration and launch playbooks. Values in
angle brackets (`<flavor>`, `<network-uuid>`, ...) are placeholders. Use the `bin/` platform wrappers for environment-variable knobs
combined with explicit Ansible arguments, or `./bin/run-playbook.sh` for full
direct playbook control. For a guided launch that generates, reviews, saves, or runs the
wrapper command, use `./bin/launch-aws-dialog.sh`, `./bin/launch-gcp-dialog.sh`,
or `./bin/launch-openstack-dialog.sh`.

Prerequisites for every example:

- Vaults under `vaults/` are populated and encrypted (see README "Secrets").
- Collections from `requirements.yml` are installed.
- For OpenStack launches, a completed OpenStack migration for `vm_name` already
  exists in `catalogs/migration-catalog.json` (or you pass image IDs explicitly).

---

## OpenStack Launch Matrix

At the playbook and environment-variable wrapper layers, OpenStack networking
is controlled by four independent switches. Combine them as needed; the table
is the quick map, and the sections below contain full commands. The interactive
`bin/launch-openstack-dialog.sh` intentionally narrows one combination: choosing
static addressing sets `OS_AUTO_IP=false` and skips the floating-IP
question. Use the wrapper or playbook directly if a fixed address and floating
IP are both required.

| Goal                       | MAC    | Address | Floating IP  | Notes                              |
| -------------------------- | ------ | ------- | ------------ | ---------------------------------- |
| Default DHCP + floating IP | off    | DHCP    | on           | Wrapper defaults                   |
| Private DHCP only          | off    | DHCP    | off          | `OS_AUTO_IP=false`                 |
| Preserve source MAC, DHCP  | on     | DHCP    | optional     | Needs free MAC + SG                |
| Fixed IP, new MAC          | off    | fixed   | off (dialog) | One IP per NIC                     |
| Preserve MAC + fixed IP    | on     | fixed   | off (dialog) | Typical "looks like old VM" launch |
| Multi-NIC                  | either | either  | optional     | `openstack_nics` / `OS_NICS_JSON`  |

Important shared rules for any launch that pre-creates Neutron ports (MAC and/or
fixed IP):

- Supply at least one security group (name or ID). Groups are applied on the
  port at create time; server-level groups are not retrofitted onto pre-made
  ports.
- The source VMware MAC must be free on the target network (power off or remove
  the source VM from that L2 domain first).
- Creating a port with an explicit MAC needs Neutron `create_port:mac_address`
  permission (project member on a self-owned network, or admin / relaxed policy
  on a shared external network such as `Ext-Net`).
- Pre-created ports are **not** deleted when the server is deleted later. Remove
  them before reusing the MAC or fixed IP (`openstack port delete ...`, or check
  `bin/reconcile-catalog-openstack.py --dry-run` for orphan port IDs).
- Fixed IPs are never discovered from the guest; you must pass them explicitly.

Playbook option names and `bin/launch-openstack-instance.sh` environment
variables (`openstack_volume_type` and `openstack_availability_zone` are
playbook-only; pass them with `-e`):

| Playbook `-e`                                | Wrapper env                    |
| -------------------------------------------- | ------------------------------ |
| `vm_name`                                    | `VM_NAME`                      |
| `openstack_flavor`                           | `OS_FLAVOR`                    |
| `openstack_key_name`                         | `OS_KEY_NAME`                  |
| `openstack_nics`                             | `OS_NICS_JSON`                 |
| `openstack_preserve_mac`                     | `OS_PRESERVE_MAC`              |
| `openstack_mac_addresses`                    | `OS_MAC_ADDRESSES_JSON`        |
| `openstack_fixed_ips`                        | `OS_FIXED_IPS_JSON`            |
| `openstack_auto_ip`                          | `OS_AUTO_IP`                   |
| `openstack_security_groups`                  | `OS_SECURITY_GROUPS_JSON`      |
| `openstack_security_group_ids`               | `OS_SECURITY_GROUP_IDS_JSON`   |
| `openstack_network` / `openstack_network_id` | `OS_NETWORK` / `OS_NETWORK_ID` |
| `openstack_config_drive`                     | `OS_CONFIG_DRIVE`              |
| `openstack_user_data`                        | `OS_USER_DATA`                 |
| `openstack_user_data_file`                   | `OS_USER_DATA_FILE`            |
| `openstack_dhcp_user_data`                   | `OS_DHCP_USER_DATA`            |
| `openstack_target_project`                   | `OS_PROJECT_NAME`              |

`OS_NICS_JSON` is a JSON list of `{"net-id": ...}` or `{"net-name": ...}` maps;
when it is set, `OS_NETWORK` is ignored. `openstack_mac_addresses` requires
`openstack_preserve_mac=true`; the launch fails rather than ignoring the MACs.

### 1. DHCP address, random MAC, floating IP (default wrapper shape)

Neutron assigns a fresh MAC and a DHCP lease. The wrapper also requests a
floating IP (`OS_AUTO_IP` defaults to `true`).

```bash
# Wrapper
VM_NAME=my-vm \
  OS_FLAVOR=m1.medium \
  OS_NETWORK=Ext-Net \
  OS_KEY_NAME=my-keypair \
  OS_SECURITY_GROUPS_JSON='["default","ssh"]' \
  OS_PROJECT_NAME=my-project-a \
  ./bin/launch-openstack-instance.sh

# Equivalent playbook invocation
./bin/run-playbook.sh playbooks/launch-openstack-instance.yml \
  -e vm_name=my-vm \
  -e openstack_target_project=my-project-a \
  -e openstack_flavor=m1.medium \
  -e openstack_network=Ext-Net \
  -e openstack_key_name=my-keypair \
  -e '{"openstack_security_groups": ["default", "ssh"]}' \
  -e openstack_auto_ip=true \
  -e openstack_dhcp_user_data=false
```

Deterministic UUIDs instead of names:

```bash
VM_NAME=my-vm \
  OS_FLAVOR=m1.medium \
  OS_NETWORK_ID='<network-uuid>' \
  OS_SECURITY_GROUP_IDS_JSON='["<security-group-uuid>"]' \
  OS_AUTO_IP=true \
  ./bin/launch-openstack-instance.sh
```

### 2. DHCP address, random MAC, no floating IP

Same as above but keep the instance on its private/fixed Neutron address only.

```bash
VM_NAME=my-vm \
  OS_FLAVOR=m1.medium \
  OS_AUTO_IP=false \
  ./bin/launch-openstack-instance.sh

# or
./bin/run-playbook.sh playbooks/launch-openstack-instance.yml \
  -e vm_name=my-vm \
  -e openstack_flavor=m1.medium \
  -e openstack_network=Ext-Net \
  -e '{"openstack_security_groups": ["default"]}' \
  -e openstack_auto_ip=false
```

### 3. DHCP address, preserved source MAC

Uses `source_mac_addresses` from the latest migration catalog entry for this
VM. Override with an explicit list when the catalog row predates MAC capture or
you need different values.

```bash
# MACs from the migration manifest
VM_NAME=my-vm \
  OS_FLAVOR=m1.medium \
  OS_PRESERVE_MAC=true \
  OS_SECURITY_GROUPS_JSON='["default","ssh"]' \
  OS_AUTO_IP=true \
  ./bin/launch-openstack-instance.sh

# Explicit MAC override (one address per NIC, in NIC order)
VM_NAME=my-vm \
  OS_FLAVOR=m1.medium \
  OS_PRESERVE_MAC=true \
  OS_MAC_ADDRESSES_JSON='["00:50:56:aa:bb:cc"]' \
  OS_SECURITY_GROUPS_JSON='["default","ssh"]' \
  ./bin/launch-openstack-instance.sh

# Playbook form
./bin/run-playbook.sh playbooks/launch-openstack-instance.yml \
  -e vm_name=my-vm \
  -e openstack_flavor=m1.medium \
  -e openstack_network=Ext-Net \
  -e openstack_preserve_mac=true \
  -e '{"openstack_mac_addresses": ["00:50:56:aa:bb:cc"]}' \
  -e '{"openstack_security_groups": ["default", "ssh"]}' \
  -e openstack_auto_ip=true
```

Read the source MAC from vSphere without re-migrating:

```bash
govc vm.info -json "/Datacenter/vm/path/my-vm" \
  | jq -r '.virtualMachines[0].config.hardware.device[]
           | select(.macAddress != null)
           | .macAddress'
```

### 4. Fixed IP, random MAC

Pre-creates a port with the requested address; Neutron still picks the MAC.
Pass exactly one IP per NIC. Turn floating IP off unless you also want a public
address on top of the fixed one.

```bash
VM_NAME=my-vm \
  OS_FLAVOR=m1.medium \
  OS_FIXED_IPS_JSON='["192.0.2.25"]' \
  OS_AUTO_IP=false \
  OS_SECURITY_GROUPS_JSON='["default","ssh"]' \
  ./bin/launch-openstack-instance.sh

./bin/run-playbook.sh playbooks/launch-openstack-instance.yml \
  -e vm_name=my-vm \
  -e openstack_flavor=m1.medium \
  -e openstack_network=Ext-Net \
  -e '{"openstack_fixed_ips": ["192.0.2.25"]}' \
  -e '{"openstack_security_groups": ["default", "ssh"]}' \
  -e openstack_auto_ip=false
```

### 5. Fixed IP and preserved MAC ("replace the old VM")

Most common shape when the guest or upstream systems key off both identity
fields.

```bash
VM_NAME=my-vm \
  OS_FLAVOR=m1.medium \
  OS_PRESERVE_MAC=true \
  OS_MAC_ADDRESSES_JSON='["00:50:56:aa:bb:cc"]' \
  OS_FIXED_IPS_JSON='["192.0.2.25"]' \
  OS_AUTO_IP=false \
  OS_SECURITY_GROUPS_JSON='["default","ssh"]' \
  ./bin/launch-openstack-instance.sh

./bin/run-playbook.sh playbooks/launch-openstack-instance.yml \
  -e vm_name=my-vm \
  -e openstack_flavor=m1.medium \
  -e 'openstack_network_id=<network-uuid>' \
  -e openstack_preserve_mac=true \
  -e '{"openstack_mac_addresses": ["00:50:56:aa:bb:cc"]}' \
  -e '{"openstack_fixed_ips": ["192.0.2.25"]}' \
  -e '{"openstack_security_group_ids": ["<security-group-uuid>"]}' \
  -e openstack_auto_ip=false \
  -e openstack_config_drive=true
```

### 6. Multi-NIC launches

Provide `openstack_nics` (list of `net-id` or `net-name` maps) **or**
`openstack_network` / `openstack_network_id`, never both. When preserving MACs
or fixing IPs, list length must equal the NIC count, in the same order.

```bash
./bin/run-playbook.sh playbooks/launch-openstack-instance.yml \
  -e vm_name=my-vm \
  -e openstack_flavor=m1.medium \
  -e openstack_network='' \
  -e '{"openstack_nics": [{"net-id": "<net-a-uuid>"}, {"net-id": "<net-b-uuid>"}]}' \
  -e openstack_preserve_mac=true \
  -e '{"openstack_mac_addresses": ["00:50:56:aa:bb:01", "00:50:56:aa:bb:02"]}' \
  -e '{"openstack_fixed_ips": ["192.0.2.25", "10.0.0.25"]}' \
  -e '{"openstack_security_groups": ["default", "ssh"]}' \
  -e openstack_auto_ip=false
```

`net-name` entries are resolved to IDs before port creation; ambiguous names
fail with candidate UUIDs. The same launch through the wrapper:

```bash
VM_NAME=my-vm \
  OS_FLAVOR=m1.medium \
  OS_NICS_JSON='[{"net-id": "<net-a-uuid>"}, {"net-id": "<net-b-uuid>"}]' \
  OS_PRESERVE_MAC=true \
  OS_MAC_ADDRESSES_JSON='["00:50:56:aa:bb:01", "00:50:56:aa:bb:02"]' \
  OS_FIXED_IPS_JSON='["192.0.2.25", "10.0.0.25"]' \
  OS_SECURITY_GROUPS_JSON='["default", "ssh"]' \
  OS_AUTO_IP=false \
  ./bin/launch-openstack-instance.sh
```

### 7. Config drive and user-data

A `network:` block in ordinary cloud-init user-data does **not** reconfigure
OpenStack guest networking. Prefer Neutron DHCP and provide network metadata
through the metadata service or config drive. Config drive carries that
metadata; it does not make network configuration in user-data take effect. Use
custom user-data for packages, users, and other non-network bootstrap only.

```bash
# Metadata service unavailable: put network metadata on the config drive
VM_NAME=my-vm \
  OS_FLAVOR=m1.medium \
  OS_CONFIG_DRIVE=true \
  OS_AUTO_IP=true \
  ./bin/launch-openstack-instance.sh

# Optional custom cloud-config for non-network initialization tasks
VM_NAME=my-vm \
  OS_FLAVOR=m1.medium \
  OS_USER_DATA_FILE=/path/to/cloud-config.yml \
  OS_CONFIG_DRIVE=true \
  ./bin/launch-openstack-instance.sh

./bin/run-playbook.sh playbooks/launch-openstack-instance.yml \
  -e vm_name=my-vm \
  -e openstack_flavor=m1.medium \
  -e openstack_network=Ext-Net \
  -e '{"openstack_security_groups": ["default"]}' \
  -e openstack_config_drive=true \
  -e openstack_user_data_file=/path/to/cloud-config.yml \
  -e openstack_dhcp_user_data=false
```

The deprecated `openstack_dhcp_user_data=true` flag attaches the historical
`examples/openstack-dhcp-user-data.yaml` sample as ordinary user-data. It does
not configure DHCP; leave it false and use Neutron DHCP plus network metadata.

### 8. Verify after launch

```bash
# Server and addresses
openstack server show '<server-name-or-id>' -f json \
  | jq '{name, status, addresses, addresses_mac: .addresses}'

# Ports attached to the server (MAC + fixed IP live here)
openstack port list --server '<server-id>' -c ID -c MAC_Address -c Fixed_IPs

# Catalog row written by the launcher
jq --arg vm my-vm \
  '.deployments[] | select(.vm_name == $vm and .target_platform == "openstack")' \
  catalogs/deployment-catalog.json
```

When cleaning up a MAC/fixed-IP instance, delete the server **and** its ports
before the next preserve-MAC launch:

```bash
openstack server delete '<server-id>'
# port IDs are in the deployment catalog under openstack_port_ids / openstack_nics
openstack port delete '<port-id-1>' '<port-id-2>'  # repeat IDs as needed
```

---

## OpenStack Migrate

Export, convert, and upload disks as Glance images; records the migration
catalog entry (including `source_mac_addresses` when govc reports them).

```bash
# Wrapper
VM_NAME=my-vm \
  VCENTER_DATACENTER=Datacenter \
  OS_PROJECT_NAME=my-project-a \
  ./bin/migrate-openstack-vm.sh

# Playbook
./bin/run-playbook.sh playbooks/migrate-openstack-vm.yml \
  -e vm_name=my-vm \
  -e datacenter=Datacenter \
  -e openstack_target_project=my-project-a

# BIOS guest (default is uefi-aware import)
./bin/run-playbook.sh playbooks/migrate-openstack-vm.yml \
  -e vm_name=my-vm \
  -e datacenter=Datacenter \
  -e openstack_target_project=my-project-a \
  -e openstack_boot_mode=bios

# Resume a failed run that retained artifacts
./bin/run-playbook.sh playbooks/migrate-openstack-vm.yml \
  -e vm_name=my-vm \
  -e datacenter=Datacenter \
  -e openstack_target_project=my-project-a \
  -e 'resume_run_id=<migration-run-id>'
```

---

## AWS

```bash
# One-time per account/region (bucket + vmimport role)
./bin/run-playbook.sh playbooks/bootstrap-aws-import.yml

# Migrate
VM_NAME=my-vm VCENTER_DATACENTER=Datacenter ./bin/migrate-aws-vm.sh

./bin/run-playbook.sh playbooks/migrate-aws-vm.yml \
  -e vm_name=my-vm \
  -e datacenter=Datacenter

# Launch (guided: ./bin/launch-aws-dialog.sh)
VM_NAME=my-vm \
  AWS_SUBNET_ID=subnet-0123456789abcdef0 \
  AWS_SECURITY_GROUPS_JSON='["sg-0123456789abcdef0"]' \
  AWS_KEY_NAME=my-ssh-key \
  ./bin/launch-aws-instance.sh

./bin/run-playbook.sh playbooks/launch-aws-instance.yml \
  -e vm_name=my-vm \
  -e vpc_subnet_id=subnet-0123456789abcdef0 \
  -e '{"security_groups": ["sg-0123456789abcdef0"]}' \
  -e key_name=my-ssh-key \
  -e assign_public_ip=true
```

---

## GCP

```bash
# Migrate (M2VM image import; boot disk gets OS adaptation)
VM_NAME=my-vm VCENTER_DATACENTER=Datacenter ./bin/migrate-gcp-vm.sh

./bin/run-playbook.sh playbooks/migrate-gcp-vm.yml \
  -e vm_name=my-vm \
  -e datacenter=Datacenter

# Launch (guided: ./bin/launch-gcp-dialog.sh)
./bin/run-playbook.sh playbooks/launch-gcp-instance.yml \
  -e vm_name=my-vm \
  -e gcp_zone=us-central1-a \
  -e gcp_machine_type=e2-standard-2 \
  -e gcp_network=default

# Wrapper
VM_NAME=my-vm \
  GCP_ZONE=us-central1-a \
  GCP_MACHINE_TYPE=e2-standard-2 \
  GCP_NETWORK=default \
  ./bin/launch-gcp-instance.sh
```

---

## Catalog Reconciliation

Dry-run first. OpenStack reconciliation is one project per invocation and will
list orphan network-identity ports when a server is gone but its ports remain.

The OpenStack script needs credentials selected first (AWS and GCP find theirs
automatically): pass `--cloud <clouds.yaml-entry>`, export `OS_CLOUD`, or
`source` an OpenStack RC file. See the README's "OpenStack Catalog
Reconciliation" section.

```bash
# Option A: source an RC file for the project being reconciled
source ~/my-project-a-openrc.sh
./bin/reconcile-catalog-openstack.py \
  --region-name US-WEST-OR-1 \
  --project-id my-project-a \
  --dry-run

# Option B: name a clouds.yaml entry instead
./bin/reconcile-catalog-openstack.py --cloud my-cloud \
  --region-name US-WEST-OR-1 \
  --project-id my-project-a \
  --dry-run

./bin/reconcile-catalog-aws.py --region us-west-2 --dry-run
./bin/reconcile-catalog-gcp.py --dry-run
```

---

## Tips

- Pass list-shaped extra-vars as **native JSON**
  (`-e '{"openstack_fixed_ips": ["192.0.2.25"]}'`) or through the wrapper env
  vars. Bare `-e openstack_fixed_ips=[...]` is accepted by the playbook's
  normaliser, but native JSON avoids edge cases in other tools. The AWS
  (`security_groups`) and GCP (`gcp_tags`) launch plays normalise the same way,
  so both forms work on all three targets; a value that is neither a list nor
  list-shaped JSON fails the play's assertion rather than reaching the module.
- `tests/validate.sh` is the static gate (syntax, vaults, unit tests). It does
  not talk to vSphere or any cloud.
- Deeper networking notes live in the README section
  [OpenStack Launch Networking](../README.md#openstack-launch-networking).
