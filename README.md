# migrate-vmware

<!-- markdownlint-disable MD013 -->

Migrate a vSphere VM to AWS, OpenStack, or GCP. Every migration uses a
quiesced snapshot, a full clone, and an OVF/OVA export; each target then imports
the resulting disk artifacts using its native image workflow. The source VM is
never powered off - only the clone is exported, so migration does not require
an outage window on the VM being migrated.

The playbooks run directly on the control host using Ansible, `govc`, and
`ovftool`.

## Prerequisites

- VMware OVF Tool. This repository is tested with the 5.1.0 Linux ZIP;
  Broadcom currently publishes 9.1.1, but newer releases are not part of the
  validated toolchain yet. Download the tested **OVF Tool for Linux Zip** from
  Broadcom's [OVF Tool 5.1.0 download page](https://developer.broadcom.com/tools/open-virtualization-format-ovf-tool/5.1.0)
  after signing in and accepting Broadcom's license terms. VMware/Broadcom's license does not permit
  redistribution, so the ZIP is **not included in this repo**.

  Install the tool on the control host according to Broadcom's instructions
  and verify that `ovftool` is available on `PATH`:

  ```bash
  ovftool --version
  ```

- Access to vCenter and to each destination cloud you use, with accounts
  that already hold the privileges listed under
  [Required Privileges](#required-privileges). AWS migrations
  require permission to create the VM import IAM role and policy, use the
  staging S3 bucket, and run `ec2:ImportImage`. OpenStack migrations require
  image, volume, network, and server permissions. Before the first GCP image
  import, enable the required Google Cloud APIs in the project selected by
  `gcp_project` and grant the permissions described under
  [Required Privileges](#required-privileges). Follow
  [Google’s M2VM service setup guide](https://docs.cloud.google.com/migrate/virtual-machines/docs/5.0/get-started/enable-services)
  for the VM Migration and supporting APIs; this workflow also uses Cloud Storage
  for staging and Cloud Build during image import. Cloud Build may be enabled
  automatically on first use. For example, an administrator can enable them with:

  ```bash
  gcloud services enable \
    vmmigration.googleapis.com \
    servicemanagement.googleapis.com \
    servicecontrol.googleapis.com \
    iam.googleapis.com \
    cloudresourcemanager.googleapis.com \
    compute.googleapis.com \
    storage.googleapis.com \
    cloudbuild.googleapis.com \
    --project=my-project-id
  ```

- Network access from the control host to vCenter and the selected cloud APIs.
- Ansible, the `amazon.aws`, `community.vmware`, and `openstack.cloud`
  collections; `govc`, `gcloud`, the AWS CLI (`aws`), `qemu-img`, and the Python
  AWS/vSphere libraries. The `gcloud` and `aws`
  CLIs stage the large disk artifacts because both upload in parallel chunks
  and resume a failed part, which the collections' upload modules do not. The control host also needs the Python dependencies
  used by the installed collections, including `boto3`, `pyVmomi`,
  `openstacksdk`, `google-auth`, `requests`, and `PyYAML`.
  `govc` should be installed as `/usr/local/bin/govc` or otherwise available
  on `PATH`.
- For the datastore-direct OpenStack migration
  ([doc/datastore-direct.md](doc/datastore-direct.md)): `nbdkit`, whose `curl`
  plugin is the fallback backend. **Install the `vddk` backend too**: nbdkit's
  `vddk` plugin and a VixDiskLib. It is several times faster (it skips a thin
  disk's unallocated space, where curl reads every byte of the flat file), and
  it is the only backend that can read a powered-on VM on VMFS. Some nbdkit
  packages, Ubuntu's among them, do not include the `vddk` plugin; the doc shows
  how to build it. It is tested with `libvixDiskLib.so.8.0.3` from
  [OpenVDDK](https://openvddk.com/), an independent, read-only replacement for
  VMware's disk library from the creator of V2K Migrate (see its site for
  availability); VMware's own VDDK works too where you are licensed for it.
- `dialog` or `whiptail` is required only for the interactive dialogs
  (`bin/*-dialog.sh`; see [Interactive Dialogs](#interactive-dialogs)) and
  should be available on `PATH`.
- Azure is **not supported yet**. `vaults/azure.yml` and `bin/azure-vault.sh`
  are reserved for in-progress Azure work; there is no Azure playbook, wrapper,
  or workflow, so nothing here needs the Azure collection or the `az` CLI, and
  neither is required to validate the repository.

## Required Privileges

These playbooks do not create accounts or grant access. Each credential in
`vaults/` must already be allowed to do what that stage needs. The lists below
describe the operations the playbooks perform, as a starting point for your
administrator; they are not a tested minimal policy, so tighten or adjust them
for your environment. Migration and launch can use different credentials, and
the AWS bootstrap is a one-time administrative step.

- **vCenter** (`vcenter_username`): read access to the inventory and the
  source VM's datastore; permission to create and remove a snapshot on the VM
  (`VirtualMachine.State.CreateSnapshot` and `RemoveSnapshot`); and permission
  to export it as an OVA (`vApp.Export`). Nothing is written to vCenter other
  than the temporary snapshot, which the run removes.
- **AWS**:
  - _Bootstrap_ (`bootstrap-aws-import.yml`, once): create the S3 staging
    bucket and the `vmimport` IAM role and its inline policy
    (`s3:CreateBucket`, `iam:CreateRole`, `iam:PutRolePolicy`, plus the
    matching `Get`/`List` calls to check for existing resources).
  - _Migration_: write, read, and delete objects in the staging bucket;
    `ec2:ImportImage`, `ec2:DescribeImportImageTasks`, and tagging the
    resulting image and snapshot.
  - _Launch_: `ec2:RunInstances` and tagging, and read access to the images,
    subnets, and security groups you name.
- **GCP** (`service_account_key`): in the project selected by `gcp_project`,
  upload, list, and delete objects in `gcs_bucket`; create, read, and delete
  Compute images; create, read, and delete VM Migration image imports (the
  Cloud Build service account used by the import needs its usual access to
  the bucket and Compute); and, for launches, create instances and disks,
  attach disks, and act as the instance's service account. Predefined roles
  that cover this include Storage Object Admin, Compute Image User/Admin,
  Compute Instance Admin (v1), VM Migration Admin, and Service Account User.
- **OpenStack** (the `openstack_projects` entry): within the target project,
  create, upload, and delete Glance images; create and delete Cinder volumes;
  read networks and security groups; and create Neutron ports and Nova servers.
  Creating ports with explicit MAC addresses (`openstack_preserve_mac=true`)
  is restricted to administrators by default Neutron policy, so that option
  needs either an admin-capable account or a relaxed policy.
- **Control host**: write access to `artifact_root` with enough free space for
  the exported OVA and converted disks, and network reach to vCenter and the
  cloud APIs.

## One-time Setup

Install the required host tools, then verify them:

```bash
ansible-galaxy collection install -r requirements.yml
ansible --version
govc version
ovftool --version
qemu-img --version   # OpenStack migrations
nbdkit --version     # datastore-direct OpenStack migrations
nbdkit vddk --dump-plugin | grep vddk_library_version   # its faster VixDiskLib backend (recommended)
gcloud --version     # GCP migrations
aws --version        # AWS migrations
command -v dialog || command -v whiptail   # interactive dialogs only
```

Collections installed outside the default path should be installed into the
same Python environment that runs Ansible. Azure-specific prerequisites (the
`az` CLI, the `azure.azcollection` Python requirements, and Azure vault values)
will be documented with the Azure workflow when it is added.

The vault helpers (`bin/*-vault.sh`) prefer the `ansible-vault` in
`$DIB7_VENV_BIN` (default `~/.dib7/bin`) and otherwise use the one on `PATH`.

## Flow

There are separate workflows for AWS, OpenStack, and GCP. The complete
flow is illustrated in [doc/workflow.md](doc/workflow.md).

1. **`playbooks/bootstrap-aws-import.yml`** - run once per AWS account/region
   (or whenever `s3_bucket`/`vmimport_role_name` change). Creates the S3
   staging bucket and the `vmimport` IAM role.

2. **`playbooks/migrate-aws-vm.yml`** - exports a VMware VM to an AWS AMI.

3. **`playbooks/launch-aws-instance.yml`** - launches an EC2 instance from an
   AWS migration catalog entry.

4. **`playbooks/stage-v2v.yml`** (optional, via `bin/stage-v2v.sh`) - run
   before `migrate-openstack-vm.yml` on CentOS 7 guests with static IP
   addresses, so they come up on DHCP in OpenStack (see "Staging CentOS 7
   Guests for OpenStack" below).

5. **`playbooks/migrate-openstack-vm.yml`** - exports a VMware VM as an OVF
   package, then converts each VMDK to QCOW2 and uploads it as a separate
   Glance image; the next disk converts while the current one uploads. The
   first disk is the boot-disk candidate; the remaining images are data
   disks. An alternative that needs no clone and no OVF export is
   **`playbooks/migrate-openstack-vm-vddk.yml`**, which snapshots the running VM
   and streams its disks straight from the datastore (see "Datastore-Direct
   Migration to OpenStack" below).

6. **`playbooks/launch-openstack-instance.yml`** - creates an OpenStack server
   from the latest completed OpenStack migration, using the first image as the
   boot disk and creating/attaching Cinder volumes for the remaining disks.
   Optional Neutron port pre-creation can preserve source MAC addresses and/or
   request fixed IPs (see "OpenStack Launch Networking" below).

7. **`playbooks/migrate-gcp-vm.yml`** - exports a VMware VM as an OVF package,
   uploads each referenced VMDK to Cloud Storage, and imports each disk into Compute Engine
   through Migrate to Virtual Machines (`gcloud compute migration image-imports`).
   The first disk receives guest OS adaptation; data disks use the same image
   import API with adaptation skipped.

8. **`playbooks/launch-gcp-instance.yml`** - launches a Compute Engine instance
   from the latest completed GCP migration and creates/attaches persistent
   disks for the migrated data images.

9. **`bin/reconcile-catalog-openstack.py`** - removes completed OpenStack
   migration entries whose Glance images are gone, and active OpenStack
   deployment entries whose servers or data volumes are gone. When a server is
   missing but its pre-created network-identity ports remain, the dry-run output
   reports those orphan ports so their MACs/fixed IPs can be freed. It supports
   `--dry-run`, `--project-id`, region selection, and the same `--force` safety
   guard (one project per run).

10. **`bin/reconcile-catalog-gcp.py`** - removes completed GCP migration entries
    whose Compute Engine images are gone, and active GCP deployment entries
    whose instances or data disks are gone. It supports `--dry-run`,
    `--project-id`, and refuses unusually large removals unless `--force` is
    supplied.

11. **`bin/reconcile-catalog-aws.py`** - removes completed AWS migration entries
    whose AMIs are gone, and active AWS deployment entries whose EC2 instances
    are gone or terminated. It supports `--dry-run`, region/profile selection,
    `--project-id`/`--account-id`, account verification, and the same `--force`
    safety guard.

The migration playbooks are thin target entry points. Shared vSphere
preflight, snapshot, clone, OVF/OVA export, manifest inspection, catalog append,
and cleanup tasks live under `playbooks/tasks/migration/common/`. Target
preflight, import, catalog fields, and artifact cleanup live under sibling
`aws/`, `openstack/`, and `gcp/` directories, each of which reuses the common
pipeline. Shared defaults are kept in `playbooks/vars/migration-common.yml`.

## Documentation

- [doc/vaults.md](doc/vaults.md) - vault file layouts in one place
- [doc/workflow.md](doc/workflow.md) - illustrated migration workflow
  ([SVG](doc/workflow.svg))
- [doc/datastore-direct.md](doc/datastore-direct.md) - the datastore-direct OpenStack
  migration: how it works, requirements, limits
- [MAINTENANCE-TERMS.md](MAINTENANCE-TERMS.md) - maintenance and support terms
- [migrate-vmware presentation](doc/migrate_vmware_presentation.pptx) - project presentation slides
- [migrate-vmware presentation video](doc/migrate_vmware_presentation.mp4) - recorded video of the project presentation

## Project Structure

```bash
migrate-vmware/
├── ansible.cfg                  # Ansible configuration
├── bin/                         # Utility scripts
│   ├── run-playbook.sh          # Playbook runner (cd's to the repo, calls ansible-run.sh)
│   ├── ansible-run.sh           # ansible-playbook wrapper that supplies the vault password
│   ├── migrate-{aws,gcp,openstack}-vm.sh  # Environment-aware migration wrappers
│   ├── migrate-*-dialog.sh      # Interactive front ends for the migration wrappers
│   ├── launch-{aws,gcp,openstack}-instance.sh  # Environment-aware launch wrappers
│   ├── launch-*-dialog.sh       # Interactive front ends for the launch wrappers
│   ├── migrate-openstack-vm-vddk.sh      # Datastore-direct OpenStack migration wrapper
│   ├── migrate-openstack-vddk-dialog.sh  # Interactive front end for it
│   ├── stage-v2v.sh             # Stages a CentOS 7 VM for DHCP before migration
│   ├── inspect-vm.py            # Disk manifest from a source VM (datastore-direct)
│   ├── enable-cbt.py            # Enables Changed Block Tracking without a power cycle
│   ├── lookup-migration-*.py    # Migration catalog lookups
│   ├── list-image-catalog.py    # Browse migrated images across all target platforms
│   ├── append-*-catalog.py      # Migration and deployment catalog appenders
│   ├── reconcile-catalog-*.py   # Removes catalog entries gone from AWS, GCP, or OpenStack
│   ├── openstack-project-facts.py  # Images, flavors, security groups, networks, keys, quotas for one project
│   ├── aws-project-facts.py     # Subnets, security groups, keypairs for one AWS account/region
│   ├── gcp-project-facts.py     # Zones, machine types, networks, subnetworks for one GCP project
│   ├── inspect-ova.py           # OVA inspection
│   └── *-vault.sh               # Vault management scripts
├── catalogs/                    # Migration and deployment catalogs
├── doc/                         # Documentation, workflow diagram, presentation
├── examples/                    # OpenStack launch matrix and sample cloud-init data
├── playbooks/                   # Ansible playbooks
│   ├── migrate-{aws,gcp,openstack}-vm.yml
│   ├── migrate-openstack-vm-vddk.yml     # Datastore-direct: no clone, no OVF
│   ├── launch-{aws,gcp,openstack}-instance.yml
│   ├── bootstrap-aws-import.yml
│   ├── stage-v2v.yml            # Installs the one-time DHCP reconfigure script
│   ├── tasks/migration/         # Per-target task pipelines (aws, gcp, openstack, common)
│   ├── templates/               # Files installed on guests
│   └── vars/                    # Shared variables
├── requirements.yml             # Pinned Ansible collections
├── tests/                       # Static validation gate and catalog tests
└── vaults/                      # Encrypted credentials
```

The platform wrappers also accept normal Ansible arguments such as `-e` and
append `-v`.

### OpenStack Launch Networking

Copy-paste commands for every DHCP / fixed-IP and MAC / no-MAC combination live
in [examples/README.md](examples/README.md#openstack-launch-matrix). This
section explains the knobs; the examples file is the cheat sheet.

The following networking and user-data options apply only to
`playbooks/launch-openstack-instance.yml`,
`bin/launch-openstack-instance.sh`, and the interactive `bin/launch-openstack-dialog.sh`. AWS and GCP launches do not use
`openstack_auto_ip`, `openstack_dhcp_user_data`, or the OpenStack user-data
files described below.

Resource names are resolved to UUIDs before launch and must match exactly one
visible resource. If a name is duplicated or the cloud exposes inconsistent
results, the launch fails with candidate IDs. For deterministic launches,
provide `openstack_network_id` and
`openstack_security_group_ids` directly. The wrapper exposes these as
`OS_NETWORK_ID` and
`OS_SECURITY_GROUP_IDS_JSON`. `openstack_network_id` alone is enough; the
`Ext-Net` default for `openstack_network` is then not looked up. For several
NICs, `openstack_nics` (wrapper: `OS_NICS_JSON`) takes precedence over both
single-network settings, which are ignored with a note, so they need not be
cleared.

The launch refuses an `instance_name` (default: the VM name) that an existing
server in the project already uses, before creating anything. If a later step
fails, it deletes what that attempt created: the server first, then the
pre-created ports and the data volumes.

To see which names and IDs a project can use, run
`./bin/openstack-project-facts.py <project>`. It lists the images, flavors,
security groups, networks, and keypairs that project can use, filtered to the
authenticated project even when the account is an admin. `<project>` is a
vault key, `project_name`, or `project_id` from `vaults/openstack.yml`. You
can leave it out when the vault has one project or `OS_*` credentials are
already set; `--cloud <name>` uses a `clouds.yaml` entry instead (that entry
picks the project, so a `<project>` given with it is ignored with a warning),
and `--json` prints machine-readable output. External networks are listed
only when open to the project: where the `access_as_external` RBAC policies
are visible, one with policies must have one for this project or `*`.
Keypairs belong to the OpenStack user, not the project, so the keypair list is
the authenticated user's.

Flavors are listed when enabled and either public or private with access
granted to the project, smallest first. The `FITS` column says whether one more
server of that flavor fits the project's remaining instance, core, and RAM
quota. Run `./bin/openstack-project-facts.py <project> --flavors` to see only
the flavors; `--security-groups`, `--networks`, `--keypairs`, and `--quotas`
narrow to those sections instead, and can be combined.

The same script ends with the project's quotas as `used/total` for compute
(instances, cores, RAM, server groups), volumes (count, GB, snapshots,
backups, and any per-volume-type limit), and network (floating IPs, networks,
subnets, ports, routers, security groups and rules). A quota is flagged `LOW`
at 80% used (`--warn-percent N` changes that), `FULL` at its limit, and
`none allowed` when the limit is 0. After a deployment, run
`./bin/openstack-project-facts.py <project> --quotas` to see only the quotas.

For interactive access to a launched OpenStack VM, provide an OpenStack
keypair and a security group that permits TCP/22. `openstack_auto_ip`
(passed through to `openstack.cloud.server: auto_ip`) asks the cloud to
give the server a public address however it does that (typically by
allocating and attaching a floating IP). It defaults to `false` in the playbook (private/network address only);
the wrapper defaults it to `true`, and exposes the setting as
`OS_AUTO_IP`. The guest image must have cloud-init
(or another image-initialization agent) installed and enabled for Nova's
keypair injection to add the public key; otherwise use credentials already
present in the source VM. The launch wrapper reads `OS_FLAVOR`,
`OS_KEY_NAME`, `OS_SECURITY_GROUPS_JSON`, and
the explicit ID variables above. `OS_KEY_NAME` defaults to a keypair named
`cloud`, which must already exist in the target project; set it to the
keypair you use. The playbook's `openstack_key_name` has no default and
launches without a keypair when it is empty.

For a guided launch, run `./bin/launch-openstack-dialog.sh`. It collects
the same common wrapper settings, shows the generated shell command, and can
save or run it after confirmation. In that interface, choosing static
addressing automatically sets `OS_AUTO_IP=false` and skips the
floating-IP question. The playbook and environment-variable wrapper still
allow fixed and floating addresses together when that is intentionally needed.

Cloud-init must be installed and enabled in the migrated guest before
launch. A `network:` block in ordinary cloud-init user-data does not
reconfigure the guest network ([cloud-init network configuration docs](https://docs.cloud-init.io/en/latest/reference/network-config.html)). OpenStack network metadata is supplied through
the metadata service or config drive, so use `openstack_config_drive=true`
when the metadata service is unavailable. The configured OpenStack network
must provide DHCP, and the guest's cloud-init datasource must be enabled to
consume that metadata.

The playbook still supports custom cloud-init user-data for other
initialization tasks. Set exactly one of:

- `-e openstack_user_data_file=path/to/cloud-config.yml` for your own file;
- `-e openstack_user_data='#cloud-config ...'` for inline content.

The two explicit sources are mutually exclusive. `openstack_dhcp_user_data`
is a deprecated compatibility option that attaches the historical `network:`
sample as ordinary user-data. Cloud-init does not apply network configuration
from user-data, so leave this option false and configure DHCP through Neutron
and the instance network metadata/configuration instead.

Recommended launch shape when using config-drive network metadata:

```bash
./bin/run-playbook.sh playbooks/launch-openstack-instance.yml \
  -e vm_name=my-vm \
  -e openstack_flavor=m1.medium \
  -e 'openstack_network_id=<network-uuid>' \
  -e openstack_security_group_ids='["<security-group-uuid>"]' \
  -e openstack_key_name=my-keypair \
  -e openstack_auto_ip=true \
  -e openstack_dhcp_user_data=false \
  -e openstack_config_drive=true
```

For a launch that must not send user-data, set
`-e openstack_dhcp_user_data=false` and leave
`openstack_user_data` and `openstack_user_data_file` unset.

The wrapper exposes the same user-data and config-drive knobs via
`OS_DHCP_USER_DATA`, `OS_USER_DATA_FILE`,
`OS_USER_DATA`, and `OS_CONFIG_DRIVE`.

MAC preservation is opt-in. Set `openstack_preserve_mac=true` to create
Neutron ports using the source VMware MAC addresses recorded in the migration
manifest. The source MAC must be free on the target network, and the launch
must have permission to create ports with explicit MAC addresses. Supply
security-group names through `openstack_security_groups` (or
`OS_SECURITY_GROUPS_JSON`); the launcher resolves unambiguous names to
IDs before creating the ports. UUIDs can also be supplied directly through
`openstack_security_group_ids` or `OS_SECURITY_GROUP_IDS_JSON`. When
MACs or fixed IPs are requested, at least one security group must be supplied
because the group is applied while the port is created. The source VM should
be powered off, or otherwise absent from the target L2 domain, before its MAC
is reused.
The wrapper exposes this as `OS_PRESERVE_MAC=true`. An optional
`openstack_mac_addresses` list (or `OS_MAC_ADDRESSES_JSON`) can override
the catalog MACs when an operator needs to provide replacement values. This is
also useful for launches from older migration catalog entries that do not yet
contain source MAC metadata; a new migration run is otherwise required.

For example:

```bash
VM_NAME=my-vm \
  OS_FLAVOR=m1.medium \
  OS_PRESERVE_MAC=true \
  OS_MAC_ADDRESSES_JSON='["00:50:56:aa:bb:cc"]' \
  ./bin/launch-openstack-instance.sh
```

Fixed IPs are independent of MAC preservation. Set
`openstack_fixed_ips` to one IP per NIC to create ports with requested fixed
addresses; Neutron will still choose the MAC unless
`openstack_preserve_mac=true` is also set. The requested fixed IP must be
available on the selected network and is not discovered automatically from the
VMware guest. The wrapper accepts fixed IPs through
`OS_FIXED_IPS_JSON`; set `OS_AUTO_IP=false` when no floating IP
is wanted:

```bash
VM_NAME=my-vm \
  OS_FLAVOR=m1.medium \
  OS_PRESERVE_MAC=true \
  OS_MAC_ADDRESSES_JSON='["00:50:56:aa:bb:cc"]' \
  OS_AUTO_IP=false \
  OS_FIXED_IPS_JSON='["192.0.2.25"]' \
  ./bin/launch-openstack-instance.sh
```

See [examples/README.md](examples/README.md#openstack-launch-matrix) for the
full matrix (DHCP-only, fixed-IP-only, multi-NIC, config-drive, and cleanup).

### AWS and GCP Launch Networking

AWS and GCP have separate launch interfaces and do not use the OpenStack DHCP
user-data or `auto_ip` settings. AWS uses `assign_public_ip` on the EC2
network interface (enabled by default in the playbook); GCP uses its selected
VPC network and subnet, with SSH access supplied by OS Login or project/
instance metadata.

`bin/launch-aws-instance.sh` passes `AWS_KEY_NAME` as the EC2 key pair, and it
defaults to a key pair named `cloud`, which must already exist in the region;
set it to the key pair you use. The playbook's `key_name` has no default and
launches without a key pair when it is empty.

GCP has no named keypair resource equivalent to AWS or OpenStack. The
equivalent of a pre-registered keypair is a `cloudinit:`-prefixed entry in the
project's `ssh-keys` metadata, which every instance in the project inherits, so
the GCP launcher needs no per-launch key argument - see "GCP SSH Access" below.

Note that `gcloud compute ssh` and the Cloud Console ephemeral-key flow rely on
`google-guest-agent`, which the Migrate to Virtual Machines OS adaptation does
not install. On migrated instances, use ordinary `ssh` with an inherited key.

AWS, OpenStack, and GCP migrations use persistent run directories under
`/var/tmp/migrate-vmware/` (the `artifact_root` default, and the wrappers'
`ARTIFACT_ROOT` default), each in a target/VM/run-ID subtree. After an
OpenStack or GCP run fails, the OVF package, manifest, and referenced VMDKs are
retained for resume. AWS retains its OVA in S3 according to its existing flow.

For large OpenStack migrations, the persistent run directory must have enough
space for the exported OVF disk set plus two temporary QCOW2 files: the disk
being uploaded and the next disk being converted alongside it. No extracted
VMDK copy is created. Set `artifact_root` (wrapper: `ARTIFACT_ROOT`) to place
the normal run hierarchy on another filesystem, or override one run's complete
directory with `migration_run_dir`:

```bash
./bin/run-playbook.sh playbooks/migrate-openstack-vm.yml \
  -e vm_name=my-large-vm \
  -e migration_run_dir=/mnt/migration-storage/my-large-vm-run
```

The OVF descriptor, checksum file, and large VMDKs stay in the run-directory
root; `disks/` contains only transient QCOW2 conversion files. Resume and
cleanup use the complete run directory. Per-disk QCOW2 files are removed after
each upload or failed per-disk attempt; the OVF package and manifest remain
available for resume unless `discard_failed_artifacts=true` is set. The playbook prints the run ID; resume
it later with:

```bash
./bin/run-playbook.sh playbooks/migrate-openstack-vm.yml \
  -e vm_name=my-vm -e datacenter=Datacenter \
  -e openstack_target_project=my-project-a -e 'resume_run_id=<run-id>'

./bin/run-playbook.sh playbooks/migrate-gcp-vm.yml \
  -e vm_name=my-vm -e datacenter=Datacenter \
  -e 'resume_run_id=<run-id>'
```

AWS does not resume with `resume_run_id`; a failed AWS import task is
cancelled while its staged OVA remains in S3 by default for a follow-up import.
If the import finished but a later step failed (the snapshot check, tagging,
or the catalog append), the run deregisters the imported AMI and deletes its
snapshots, since no catalog row will ever point at them.
The local AWS OVA is written under
`/var/tmp/migrate-vmware/aws/<vm-name>/<run-id>` by default; set
`migration_run_dir` to place this potentially large file elsewhere, for example:

```bash
./bin/run-playbook.sh playbooks/migrate-aws-vm.yml \
  -e vm_name=my-vm \
  -e migration_run_dir=/mnt/migration-storage/my-vm-run
```

Use `-e discard_failed_artifacts=true` only when the retained artifacts should
be deleted instead of being available for resume. On a resumed OpenStack run
the discard also removes the Glance images earlier attempts of the same run
uploaded, found by the run's image-name prefix. Import polling defaults to
four hours for AWS (`import_poll_retries` 480 x `import_poll_delay` 30s) and
GCP (`gcp_import_poll_retries` 960 x `gcp_import_poll_delay` 15s) and one hour
for OpenStack (`openstack_import_poll_retries` 240 x
`openstack_import_poll_delay` 15s); raise them for very large images.

GCP imports run concurrently, so when one disk fails the others may still be
importing. Without a discard they are left to finish, and a resume reuses
their images. With `discard_failed_artifacts=true` the run cancels them (a
best-effort call to the Migrate to Virtual Machines API), waits up to
`gcp_cancel_poll_retries` (default 120) x `gcp_import_poll_delay` for them to
stop, removes the import resources, and then deletes the images this attempt
created along with any it reused from an earlier attempt. An import that
never stops is reported so its image can be removed by hand.

Multi-disk VMs overlap their slow steps rather than running them end to end.
GCP starts every M2VM image import before waiting on any of them, so total
import time is the slowest disk instead of the sum of all of them. OpenStack
converts the next disk while the current one uploads to Glance, which costs one
extra staged qcow2 in `migration_run_dir/disks` and hides the shorter of the two
steps on every disk after the first; `openstack_convert_args` (default
`['-m', '16', '-W']`) tunes the qemu-img coroutine count, and
`openstack_convert_async_timeout` bounds one conversion.
`openstack_convert_args` may also be a string, so a bare
`-e openstack_convert_args='-m 8 -W'` works: a string that starts with `[` is
read as a YAML list, anything else is split on whitespace.

GCP imports the boot disk through M2VM, so the guest OS must be on Google's
[Migrate to Virtual Machines supported-OS list](https://docs.cloud.google.com/migrate/virtual-machines/docs/5.0/discover/supported-os-versions)
(M2VM also rewrites the guest for Compute Engine during "OS adaptation").
Google documents support for 64-bit x86 Linux and Windows Server migrations;
image names embed the run ID and
are truncated to Compute Engine's 63-character limit; because the run ID is
unique per run, existing images are replaced by default
(`gcp_replace_existing_image=true`). Staging VMDK files land in
`gs://<gcs_bucket>/migrate-vmware/<run-id>/` and are deleted after a
successful migration, retained for debugging after a failed one.

Failed AWS migrations cancel an unfinished import but retain the staged OVA in
S3 by default, allowing a follow-up import without another export and upload.
Pass `-e discard_failed_artifacts=true` to delete the S3 object on failure.

Useful migration options include `validate_certs`, `quiesce_snapshot`, and
`govc_bin`; AWS polling uses `import_poll_retries`/`import_poll_delay`. For an
AWS source with known firmware, set `aws_import_boot_mode` to `uefi` or
`legacy-bios`; when omitted, AWS receives no explicit mode and can infer it.
OpenStack and GCP use their target-specific polling variables. OpenStack also
supports `openstack_boot_mode` (use `bios` for BIOS guests),
`openstack_nics`, `openstack_preserve_mac`, `openstack_mac_addresses`,
`openstack_fixed_ips`, and `openstack_auto_ip`, plus the playbook-only
`openstack_volume_type` and `openstack_availability_zone` (no wrapper
environment variable; pass them with `-e`). GCP launch accepts `instance_name`,
`gcp_disk_type` and the network/tag options. AWS launch accepts
`assign_public_ip` and `extra_tags`. When manually supplying multiple
OpenStack image IDs, also supply `source_artifact_manifest` with a
`capacity_bytes` entry for every disk.
Successful migrations always delete their staging object.
AWS also refuses to upload when the boot disk is not the first disk listed in
the OVF, because VM Import picks the root volume from the descriptor order (see
"Export Artifact Manifest"); `-e aws_allow_boot_order_mismatch=true` overrides
that check. After the import, the count of imported snapshots must equal
the manifest's disk count.

```bash
./bin/run-playbook.sh playbooks/bootstrap-aws-import.yml

./bin/run-playbook.sh playbooks/migrate-aws-vm.yml \
  -e vm_name=my-vm -e datacenter=Datacenter

./bin/run-playbook.sh playbooks/migrate-openstack-vm.yml \
  -e vm_name=my-vm -e datacenter=Datacenter \
  -e openstack_target_project=my-project-a

./bin/run-playbook.sh playbooks/migrate-gcp-vm.yml \
  -e vm_name=my-vm -e datacenter=Datacenter

./bin/run-playbook.sh playbooks/launch-aws-instance.yml \
  -e vm_name=my-vm \
  -e vpc_subnet_id=subnet-0123456789abcdef0 \
  -e security_groups='["sg-0123456789abcdef0"]' \
  -e key_name=my-ssh-key

./bin/run-playbook.sh playbooks/launch-openstack-instance.yml \
  -e vm_name=my-vm \
  -e openstack_target_project=my-project-a \
  -e 'openstack_flavor=<available-flavor>' \
  -e openstack_network=Ext-Net \
  -e openstack_key_name=my-keypair \
  -e openstack_security_groups='["default","ssh"]' \
  -e openstack_auto_ip=true \
  -e openstack_dhcp_user_data=false

./bin/run-playbook.sh playbooks/launch-gcp-instance.yml \
  -e vm_name=my-vm \
  -e gcp_target_project=my-project-a \
  -e gcp_zone=us-central1-a \
  -e gcp_machine_type=e2-standard-2 \
  -e gcp_network=default
```

#### GCP SSH Access

Register the standing key once at project level, the way an AWS or OpenStack
keypair is registered once in the cloud. Every launched instance then inherits
it and no per-launch key argument is needed:

```bash
# merge, do not overwrite: this metadata key holds every project SSH key
gcloud compute project-info describe --project=my-project \
  --format="value(commonInstanceMetadata.items.filter(\"key:ssh-keys\").extract(value))" \
  > /tmp/ssh-keys
printf 'cloudinit:%s\n' "$(cat ~/.ssh/id_ed25519.pub)" >> /tmp/ssh-keys
gcloud compute project-info add-metadata --project=my-project \
  --metadata-from-file=ssh-keys=/tmp/ssh-keys
```

The `cloudinit:` prefix is required. cloud-init's GCE datasource accepts only
`cloudinit` or the image's own default user (`debian`, `ubuntu`) and **silently
discards every other prefix**, including `root`, leaving an empty
`authorized_keys` with no error anywhere but the serial console. Entries for
real usernames are honored only by `google-guest-agent`, which migrated images
do not have. Where the image sets `disable_root: false`, cloud-init applies the
key to both the image's default user and `root`.

To add an extra key for a single launch, set `gcp_ssh_public_key_file`
(wrapper: `GCP_SSH_KEY_FILE`) to a public key file. It is injected as instance
`ssh-keys` metadata and cloud-init merges it with the inherited project keys
rather than replacing them. `gcp_ssh_user` (wrapper: `GCP_SSH_USER`) sets its
prefix and defaults to `cloudinit`.

Keys are applied by cloud-init's `cc_ssh` module, whose frequency is
`PER_INSTANCE`. Changing project or instance metadata therefore has no effect
on an already-launched instance, and neither does a reboot or `instances reset`:
the instance ID is unchanged, so the module does not re-run. To change an
instance's keys, delete and relaunch it.

Credentials (vCenter host/user/password, OpenStack authentication, AWS
region/bucket/role and access keys, and GCP project/service-account settings)
come from the encrypted files in `vaults/` - see
"Secrets" below. Anything still passed with `-e` overrides the vault values;
`-e aws_region=...`, `-e vcenter_hostname=...` and friends still work.

`bin/run-playbook.sh` runs the playbook locally and automatically supplies the
Ansible Vault password from `~/.ssh/dib-vault-pass`, `VAULT_PASSWORD_FILE`, or
an interactive prompt:

```bash
./bin/run-playbook.sh playbooks/bootstrap-aws-import.yml
./bin/run-playbook.sh playbooks/migrate-aws-vm.yml \
  -e vm_name=my-vm -e datacenter=Datacenter
```

The seven `bin/` platform wrappers combine their environment-variable settings with
any Ansible arguments supplied on the command line; for example,
`./bin/migrate-openstack-vm.sh -e vm_name=my-vm -e openstack_target_project=my-project`.
Migration wrappers default their artifact root to `/var/tmp/migrate-vmware`,
the same as the playbooks, and accept `ARTIFACT_ROOT` to override it. The wrappers are useful when you want the
playbook runner, vault handling, and verbosity applied consistently.

The interactive `bin/*-dialog.sh` front ends build these wrapper commands for
you; see [Interactive Dialogs](#interactive-dialogs). Ready-to-run command examples (OpenStack networking matrix, AWS, GCP,
migrate, and reconcile) are collected in
[examples/README.md](examples/README.md).

## Interactive Dialogs

Seven `bin/` scripts are guided front ends for the platform wrappers. They ask
for the settings the wrappers otherwise read from environment variables,
generate the equivalent wrapper command, and let you review, save, or run it.
Nothing runs until you confirm at the end.

| Dialog                                 | Generates a call to                |
| -------------------------------------- | ---------------------------------- |
| `bin/migrate-aws-dialog.sh`            | `bin/migrate-aws-vm.sh`            |
| `bin/migrate-gcp-dialog.sh`            | `bin/migrate-gcp-vm.sh`            |
| `bin/migrate-openstack-dialog.sh`      | `bin/migrate-openstack-vm.sh`      |
| `bin/migrate-openstack-vddk-dialog.sh` | `bin/migrate-openstack-vm-vddk.sh` |
| `bin/launch-aws-dialog.sh`             | `bin/launch-aws-instance.sh`       |
| `bin/launch-gcp-dialog.sh`             | `bin/launch-gcp-instance.sh`       |
| `bin/launch-openstack-dialog.sh`       | `bin/launch-openstack-instance.sh` |

Requirements: `dialog` (preferred) or `whiptail`, and an interactive terminal
of at least 20x10 (run the script directly, not through a pipe or redirect).

What each kind asks:

- **Migrate dialogs** pick the source vCenter and the target AWS account, GCP
  project, or OpenStack project from the keys in the matching vault, then ask
  for the source VM name, an optional certificate-validation override
  (`VALIDATE_CERTS`), and whether to quiesce the snapshot
  (`QUIESCE_SNAPSHOT`). Without a selected vCenter they also ask for the legacy
  `VCENTER_DATACENTER` override. The datastore-direct dialog
  (`migrate-openstack-vddk-dialog.sh`) also asks whether to compress the QCOW2
  images (`COMPRESS`, default no; yes if `COMPRESS=true` is already set) and
  leaves `BACKEND` at auto; add `BACKEND` to a saved script to change it.
- **Launch dialogs** pick the target project the same way and offer the
  migrated VM from `catalogs/migration-catalog.json` (newest completed
  migration for that platform first; a name can also be typed). They then ask
  for the platform's launch settings: subnet, security groups, instance type
  and key pair for AWS; zone, machine type, network, tags, external IP and
  optional SSH key for GCP; and IP, MAC, floating IP, security group,
  config-drive, user-data, flavor, key pair, and network choices for OpenStack
  (see [OpenStack Launch Networking](#openstack-launch-networking)).
  Wherever the cloud API can list valid values, the dialog offers a live
  picker (backed by `bin/openstack-project-facts.py`,
  `bin/aws-project-facts.py`, or `bin/gcp-project-facts.py`) with the
  current value pre-selected. Long single-select lists lead with a
  `Type manually (skip the list)` row so a known value can be typed without
  scrolling, and a failed lookup falls back to manual entry instead of
  aborting.

Target choices are read from the vault with `ansible-vault view`, using
`VAULT_PASSWORD_FILE` or `~/.ssh/dib-vault-pass`. If the password file or
`ansible-vault` is unavailable, the dialog falls back to typing the name; leave
it blank to let the playbook auto-select a single-entry vault.

At the end the dialog offers three steps: review the generated command, save it
(default `./migrate-<vm>.sh` or `./launch-<vm>.sh`), and run it now. The
generated script is a short shell file of `export` lines followed by the
wrapper's absolute path, so a saved copy can be run from any directory. An
unsaved command is left in `/tmp`. Cancelling at any prompt, or pressing Esc,
exits with "Nothing was run."

The migrate dialogs write `ARTIFACT_ROOT` into the script, defaulting to
`/var/tmp/migrate-vmware`; set `ARTIFACT_ROOT` before starting a dialog when the
artifacts belong on another filesystem. The shared widgets and the
terminal handling live in `bin/launch-dialog-common.sh`, which every dialog
sources. Ready-to-run examples are collected in
[examples/README.md](examples/README.md).

## Datastore-Direct Migration to OpenStack

`playbooks/migrate-openstack-vm-vddk.yml` migrates a running VM to OpenStack
without cloning it and without an OVF export: it takes a quiesced snapshot, then
streams each frozen disk from the datastore, converts it to QCOW2, and uploads it
to Glance. It writes the same catalog entries as the OVF method. Disks are read
through VixDiskLib (nbdkit's `vddk` plugin) when it is installed, and otherwise
from vCenter's `/folder` endpoint (nbdkit's `curl` plugin). Install the `vddk`
backend: curl is about four times slower (it reads every byte of each flat disk
file, empty space included, which matters most for large thin disks), needs
flat disks with no existing snapshots, and cannot read a powered-on VM on VMFS,
whose disks ESXi locks. On a slow upload
link, `COMPRESS=true` compresses the QCOW2 images as they are written, which
cut the upload by more than half in testing.

```bash
VM_NAME=my-vm VSPHERE_TARGET_VCENTER=my-vcenter ./bin/migrate-openstack-vm-vddk.sh
COMPRESS=true VM_NAME=my-vm ./bin/migrate-openstack-vm-vddk.sh      # slow upload link
BACKEND=curl VM_NAME=my-vm ./bin/migrate-openstack-vm-vddk.sh       # force the fallback
```

It refuses independent, shared, raw-mapped, and encrypted disks before creating
a snapshot. Changed Block Tracking is not required. The controller host must be
Linux: the stream bookkeeping reads `/proc` and relies on POSIX signals. See
[doc/datastore-direct.md](doc/datastore-direct.md) for how it works, installing
the `vddk` backend, the limits, and what was tested.

**Resume is unproven here.** Artifact retention, `resume_run_id`, and
`discard_run_id` are implemented, but a resumed datastore-direct run has not
been exercised against a real vCenter at this revision (see the doc's "Tested"
section). Treat `resume_run_id` as experimental until it has been verified in
your environment; `discard_run_id`, or a fresh run, are the safer options.

## Staging CentOS 7 Guests for OpenStack

When CentOS 7 servers with static IP addresses migrate to OpenStack, they may
need staging to reconfigure themselves for DHCP before the migration begins. The
stage-v2v playbook installs a one-time, boot-time reconfigure script to do this.

```bash
bin/stage-v2v.sh -c vcenter.example.com -H my-vm [-a address] [-u ssh_username] [-k ssh_private_key_file]
```

The VM is looked up in vCenter using the credentials in `vaults/vsphere.yml`. The same vault entry can
also supply the guest SSH login (`ssh_username`, `ssh_password`,
`ssh_private_key_file`); see [doc/vaults.md](doc/vaults.md#vsphere-vault).
Windows guests are detected and skipped, as are VMs that are not CentOS or not
reachable over SSH. `-a` gives an address for VMs whose vCenter name is not in
DNS. On CentOS, `playbooks/templates/stage-v2v.sh.j2` is installed as
`/etc/init.d/stage-v2v.sh` and enabled with `chkconfig`. On first boot on
OpenStack it switches the NIC to DHCP, restarts networking, and disables itself.
If the NIC's ifcfg file is missing or cannot be rewritten, it still disables
itself, leaves networking alone, and exits non-zero, so it never retries on
every boot.

## Development and Validation

Run the repository gate with:

```bash
tests/validate.sh
```

It checks playbook and shell syntax, shellcheck warnings in `bin/` and in the
shell templates under `playbooks/templates/`, file references in the Markdown
docs, vault headers, Python compilation, YAML, catalog structure, and the pinned
Ansible collection versions. It is a static/structure gate rather than a cloud
integration test. Focused unit tests exercise catalog helper behavior and the
datastore-direct disk manifest (`bin/inspect-vm.py`), but no vSphere or cloud
operations are performed. Run it before submitting changes.

Common failures:

- Missing AWS `vmimport` role or staging bucket: run
  `playbooks/bootstrap-aws-import.yml` first.
- GCP M2VM rejects the guest OS: verify the source OS is on Google's supported
  list.
- Snapshot quiescing fails because VMware Tools are unavailable: use
  `-e quiesce_snapshot=false`.
- A VM name matches more than one vSphere object: use a unique VM name.
- A retained resume artifact is reported incomplete: remove that disk artifact
  from the run directory and resume; it will be recreated atomically.
- An OpenStack resume fails because an existing Glance image is not `active`
  (an interrupted upload left it `queued`, `saving`, or `killed`): delete that
  image and resume again.
- A migration stops with "disk order is not usable" or "out of manifest disk
  order": the manifest's disks must be numbered 0..n-1 with only disk 0 as the
  boot disk, and imported image names must end `-disk-<index>` in that order.
- AWS refuses to upload because the boot disk is not first in the OVF: see
  `aws_allow_boot_order_mismatch` above.
- Datastore-direct: "nbdkit's vddk plugin is not installed" or "found no
  VixDiskLib" (with `BACKEND=vddk`, or in `auto`'s fall-back-to-curl message):
  build the plugin or link the library as
  [doc/datastore-direct.md](doc/datastore-direct.md) describes, or use
  `BACKEND=curl`.
- Datastore-direct: "vCenter would not serve ... (HTTP 500)" with the curl
  backend: the VM is powered on and its disks are on VMFS, which ESXi locks.
  Power it off, or install the `vddk` backend.
- Datastore-direct: "already has snapshots ... the curl backend cannot read
  them": delete or consolidate the VM's snapshots, or use the `vddk` backend.
- Datastore-direct: a failed run keeps its migration snapshot on the source VM
  as the resume checkpoint. Resume with the printed `resume_run_id`, or discard
  the run (snapshot, run directory, uploaded images) with
  `-e discard_run_id=<id>` (wrapper: `DISCARD_RUN_ID`); a retained snapshot keeps
  growing.
- Datastore-direct with `validate_certs: true`: "Could not verify the
  certificate": install the vCenter's CA on the control host, or pass its SHA-1
  thumbprint with `-e openstack_vddk_thumbprint=<thumbprint>`.

## Secrets

All credentials live in the Ansible-vault-encrypted files in `vaults/`. The
vault password is expected at `~/.ssh/dib-vault-pass` (any of
`--ask-vault-pass`, `--vault-password-file <file>`, `--vault-id`, or
`VAULT_PASSWORD_FILE` also works - see `bin/ansible-run.sh`). Edit (or create) a vault with its
helper, which uses the same password lookup and falls back to a prompt:

```bash
./bin/aws-vault.sh        # vaults/aws.yml
./bin/gcp-vault.sh        # vaults/gcp.yml
./bin/openstack-vault.sh  # vaults/openstack.yml
./bin/vsphere-vault.sh    # vaults/vsphere.yml
```

The helpers are symlinks to `bin/edit-vault.sh`; set `VAULT=<path>` and run
`bin/edit-vault.sh` for any other file. Plain
`ansible-vault edit vaults/aws.yml --vault-password-file ~/.ssh/dib-vault-pass`
works too.

See [doc/vaults.md](doc/vaults.md) for the vault file layouts in one place.

- **`vaults/vsphere.yml`** - `vsphere_projects`, a mapping of vCenter key to
  a vCenter entry: `vcenter_hostname`, `vcenter_username`, `vcenter_password`,
  optional `datacenter` (falls back to the pre-existing `datacenter`
  variable, default `"Datacenter"`), and optional `validate_certs`
  (default `true`) - one TLS knob per entry governing the `community.vmware`
  modules, `ovftool`, and govc (`GOVC_INSECURE` follows it unless an
  optional `govc_insecure` override is set). One run targets one vCenter,
  selected with `-e vsphere_target_vcenter=<key-or-hostname>`; the selector
  (`playbooks/tasks/migration/common/select-vcenter.yml`) matches a vault
  key or a `vcenter_hostname`, auto-selects a single-entry map, and fails
  loud listing available keys. A legacy vault holding only the flat
  `vcenter_hostname`/`vcenter_username`/`vcenter_password` still works
  unmodified: with no explicit `-e vsphere_target_vcenter` the legacy
  credentials are used as-is even when `vsphere_projects` is also present
  (the same rule as `openstack_auth`). The legacy `GOVC_URL`/`GOVC_USERNAME`/
  `GOVC_PASSWORD`/`GOVC_INSECURE` vault keys are gone: the selector derives
  them from the selected entry, so update the vault through the map (or the
  flat block) only.

  ```yaml
  vsphere_projects:
    example:
      vcenter_hostname: "vc.example.com"
      vcenter_username: "administrator@example.com"
      vcenter_password: "my-password"
      datacenter: "Datacenter"
      validate_certs: false
    other-site:
      vcenter_hostname: "vc.other.example.com"
      vcenter_username: "administrator@other.example.com"
      vcenter_password: "my-password"
      datacenter: "Other-DC"
      validate_certs: true
  ```

  The shared vault may also contain DIB7 Content Library defaults. This repo
  does not read these optional values; they are shown here to document the
  shared vault shape:

  ```yaml
  vsphere_content_library: "my-content-library"
  vsphere_template_name: "inventory-item-base.tmpl"
  ```

  The migration catalog records `source_vcenter_key` (the vault key, or
  `legacy` when the flat credentials were used) alongside `source_vcenter`
  so each entry names the vCenter it came from. The reconcile and lookup
  tools ignore it today; match on it manually when a VM exists on more than
  one vCenter.
  The `bin/migrate-*.sh` wrappers pass `VSPHERE_TARGET_VCENTER` through
  when set - the selected entry then supplies the datacenter, and
  `VCENTER_DATACENTER` is ignored (it is a legacy single-vCenter override
  that only applies when no target is selected). `VALIDATE_CERTS` is
  forwarded only when explicitly set (the vault entry decides otherwise).

- **`vaults/openstack.yml`** - `openstack_projects`, a mapping of project key
  to an OpenStack authentication mapping: `auth_url`, `username`, `password`,
  `project_name`, optional `project_id` (recommended for ID-scoped clouds),
  `user_domain_name`, and `project_domain_name`. Every key stays inside the Ansible-vault-encrypted
  file — unlike `clouds.yaml`, no clear-text password is written anywhere.
  One run targets one project, selected with
  `-e openstack_target_project=<name>`:

  The wrappers use the standard `OS_PROJECT_NAME` environment variable for
  this same selector. `OS_REGION_NAME` is also honored when a vault project
  does not specify a region. Authentication remains in the encrypted vault;
  the other `OS_*` wrapper variables documented in the launch examples are
  project-specific convenience settings, not replacements for vault secrets.

  ```yaml
  openstack_projects:
    my-project-a:
      auth_url: "https://keystone.example.com:5000/v3"
      username: "my-username"
      password: "my-password"
      project_name: "my-project-a"
      project_id: "project-id-a"
      region_name: "my-region"
      user_domain_name: "Default"
      project_domain_name: "Default"
    my-project-b:
      auth_url: "https://keystone.example.com:5000/v3"
      username: "my-username"
      password: "my-password"
      project_name: "my-project-b"
      project_id: "project-id-b"
      region_name: "my-region"
      user_domain_name: "Default"
      project_domain_name: "Default"
  ```

  Quote numeric project IDs for both the key and `project_name` so they stay
  strings. The key and its `project_name` must agree. `project_id` is optional
  and is recorded in the catalogs as `openstack_project_id` for reconcile and
  lookup matching; `-e openstack_target_project` matches a vault key, a
  `project_name`, or a `project_id` (string-compared, so quoting is belt and
  braces rather than load-bearing). `region_name` should be set for
  multi-region clouds and is recorded on the catalog entry. A legacy vault holding
  only a single `openstack_auth` mapping still works unmodified: with no
  explicit `-e openstack_target_project` the legacy mapping is used as-is even
  when `openstack_projects` is also present. A single-entry `openstack_projects`
  map is auto-selected; several entries with no request fail and ask for the
  flag.

  The legacy single-project `openstack_auth` mapping remains supported. The
  catalog reconciliation script is a separate CLI workflow: it uses the
  OpenStack SDK environment variables or a `clouds.yaml` entry rather than
  reading `vaults/openstack.yml` (see "OpenStack Catalog Reconciliation" below).

- **`vaults/aws.yml`** - `aws_projects`, a mapping of AWS account/project key to
  `aws_region`, `s3_bucket`, `vmimport_role_name`, and optionally
  `aws_account_id`, `aws_profile`, `aws_access_key_id`, `aws_secret_access_key`,
  and `aws_session_token`. Select one with `-e aws_target_project=<key-or-account-id>`;
  a single entry is selected automatically. A legacy flat mapping remains supported.
  Loaded by all three playbooks. The playbooks export
  the access keys into the task environment so the `amazon.aws` modules pick
  them up the normal boto3 way; if those entries are empty, boto3 falls back
  to a local `~/.aws` configuration or an instance profile, and the playbooks
  warn when nothing at all resolves.

  ```yaml
  aws_projects:
    "123456789012":
      aws_account_id: "123456789012"
      aws_region: "my-region"
      s3_bucket: "my-bucket"
      vmimport_role_name: "vmimport-role"
      # Optional: aws_profile, aws_access_key_id,
      # aws_secret_access_key, aws_session_token
  ```

- **`vaults/gcp.yml`** - `gcp_projects`, a mapping of project key to
  `gcp_project`, `gcs_bucket`, `gcp_import_location`, and a
  `service_account_key` JSON document for a service account with GCS, Compute,
  Cloud Build, and M2VM permissions. One run targets one project, selected with
  `-e gcp_target_project=<key-or-project-id>`; a single-entry map is selected
  automatically. A legacy flat mapping with `gcp_project`, `gcs_bucket`,
  `gcp_import_location`, and `service_account_key` remains supported. For
  example:

  ```yaml
  gcp_projects:
    my-project-a:
      gcp_project: "my-project-a"
      gcs_bucket: "my-bucket-a"
      gcp_import_location: "us-central1"
      service_account_key: |
        { ... JSON ... }
    my-project-b:
      gcp_project: "my-project-b"
      gcs_bucket: "my-bucket-b"
      gcp_import_location: "us-east1"
      service_account_key: |
        { ... JSON ... }
  ```

  `gcp_import_location` is the region the Migrate to Virtual Machines _import
  job_ runs in - the value passed to `gcloud compute migration image-imports
--location`. It does **not** control where `gcs_bucket` lives (fixed when
  the bucket is created; the import reads the staged VMDK across regions) or
  where the finished image lives (a GCE Compute image is a _global_ resource).
  It only picks which region does the conversion, so it can be changed freely,
  including to route around a regional outage:

  ```bash
  ./bin/migrate-gcp-vm.sh -e vm_name=my-vm -e gcp_import_location=us-east1
  ```

  On 2026-08-12 the import service in `us-central1` failed four consecutive
  times at the `loadingSourceFiles` step with `code 13`, "Internal migration
  service error", while the byte-identical staged object imported cleanly in
  `us-east1`. If an import fails with an internal error and no useful detail,
  retrying in another region is the fastest way to tell a bad artifact apart
  from a bad region.

Precedence is: `-e`/`--extra-vars` > vault files > playbook defaults. No
secrets flow through `.env` files or unencrypted repository files;
`vaults/env`, `secrets/vcenter_password.txt` and the old
`VCENTER_PASSWORD`/`AWS_*` passthroughs are gone.

## Export Artifact Manifest

Each migration records a manifest of the disks it uploads. Both source methods
produce the same overall shape — `manifest_version`, `artifact_type`, `disks`
numbered `0..n-1` in boot order with only disk 0 a boot candidate, and a
`virtual_disk_attachments` list — so the validation, import, catalog, and launch
stages run unchanged on either. The `artifact_type` and the per-disk fields
differ by method:

- **OVF export** (`bin/inspect-ova.py`, used by AWS, GCP, and the OVF OpenStack
  method): `artifact_type` `ovf` or `ova`, and per disk the source VMDK `path`,
  `capacity_bytes`, `populated_size_bytes` when provided, `format`,
  `ovf_position` (where the descriptor lists the disk), and the OVF attachment
  metadata (`disk_id`, `instance_id`, `address_on_parent`, `parent`).
- **Datastore-direct** (`bin/inspect-vm.py`): `artifact_type` `datastore`, and
  per disk the datastore `path`, `capacity_bytes`, `thin_provisioned`,
  `snapshot_chain_depth` (how many parent files the disk has; the `curl` backend
  refuses a non-zero depth, because it reads each disk as one flat file),
  `file_size_bytes` (null: the flat file is not measured, the conversion reads
  the backing), and an attachment keyed by the vSphere `device_key`.

Both record the ordered `source_mac_addresses` captured from vSphere, when
available, for OpenStack launches that preserve network identity. The
datastore manifest adds `source_vm_name`, `boot_order_source` (`bootOptions`
when the VM's configured boot order named a disk, otherwise `controller`), and
`transfer_backend` (which backend read the disks, so a resume reuses it).

Disks are ordered by boot order: the VM's configured
`config.bootOptions.bootOrder` first, then the disk controller's bus number
(IDE, SCSI, SATA), then the controller, then the unit on it. Disk `index`
follows that order, the first disk is marked as the only boot candidate, and
each OVF-manifest disk also records `ovf_position`. The playbooks and
`bin/append-migration-catalog.py` refuse a manifest whose indexes are not
0..n-1 or whose boot candidate is not disk 0, and AWS additionally requires the
boot disk's `ovf_position` to be 0 (see above).

The manifest is target-neutral. AWS continues to import the original OVA,
OpenStack converts individual OVF VMDKs or streams disks straight from the
datastore, and the GCP workflow
uploads the referenced VMDKs to Cloud Storage and imports them through M2VM;
each workflow then creates the boot image and additional attached disks
appropriate to its platform. AWS imported AMIs receive a `Name` tag matching
the source VM name; EC2 ImportImage does not expose a separate native AMI-name
parameter.

## Migration Catalog

Every successful AWS, OpenStack, or GCP migration run appends one record to
`catalogs/migration-catalog.json` via `bin/append-migration-catalog.py`:
`vm_name`, source vCenter/datacenter, `clone_name`, `snapshot_name`, the
source `artifact_type`, target-neutral `artifact_manifest`, target platform,
`status`, and `migrated_at`. AWS records additionally contain the S3 location,
`ami_id`, and `import_task_id`, plus `aws_account_id` for new multi-account
entries; OpenStack records contain ordered Glance image
IDs and names plus the OpenStack project (key, always recorded) and optional
project ID and region; GCP records contain the
project, import location, GCS staging bucket and prefix, the boot image name
and selfLink, and ordered data-disk image names and selfLinks. GCP migration
produces Compute Engine images and the GCP launch workflow can create an
instance with those images. Unlike a catalog that
tracks only current state, this one accumulates - AWS never
retires an AMI behind your back the way, say, an in-place vSphere template
gets overwritten, so an older record for the same `vm_name` is still a valid,
independently deployable AMI and stays in the file. Only an exact rerun of
the same `import_task_id` replaces its own prior entry. Look here rather than
scrolling back through playbook output to find an AMI ID.

Browse the migrated images with `bin/list-image-catalog.py` (no cloud credentials
or API calls needed). It reads this repository's migration catalog by default,
even when run from another directory, and shows all completed migrations, not
only the latest for each VM:

```bash
./bin/list-image-catalog.py
./bin/list-image-catalog.py --provider openstack --project my-project --region RegionOne
./bin/list-image-catalog.py --vm-name my-vm --status all
./bin/list-image-catalog.py --provider aws --project 123456789012 --region eu-west-1
./bin/list-image-catalog.py --catalog /path/to/migration-catalog.json --json
```

The report includes migration time and ID, source vCenter/datacenter,
project/account and region, and the AMI or ordered per-disk images. `--platform`
is an alias for `--provider`; `--project` matches recorded project names/keys or
IDs (AWS: account ID or project key). `--region` requires an exact recorded
region; for GCP it matches the import location, not an instance's zone. Rows
without that scope do not match a scope filter. `--status` defaults to
`completed`; use `all` to include every status. Rows are grouped by VM and
platform with newest migrations first, and `--json` returns the matching full
rows as a JSON list. This reports catalog contents without modifying them or
checking whether the images still exist in the cloud; use reconciliation for
that check.

`playbooks/launch-aws-instance.yml` reads AWS entries in this catalog through
`bin/lookup-migration-ami.py`, which picks the most recent `status: completed`
entry for the given `vm_name`; pass `--project <account-id-or-key>` when
selecting among multiple AWS accounts and `--region <region>` to restrict the
match to one region (the launch playbook passes its `aws_region`, since an AMI
cannot be launched in another region). With `--project`, rows recorded for
that account win; legacy rows without `aws_account_id` are used only when no
row for the account matches, since they could come from any account.
OpenStack entries contain an ordered list of
Glance image IDs, one per source disk, for use by the OpenStack launch
workflow; `bin/lookup-migration-openstack.py --project <name-or-id> --region <region>` picks the
newest `completed` migration for one `vm_name` in one project and region, and the launch
playbook refuses a migration from another project because Glance images and
Cinder volumes cannot move across projects. GCP entries are resolved the same
way: `bin/lookup-migration-gcp.py --project <key-or-project-id>` selects the
newest `completed` migration for one `vm_name` in one project, and the launch
playbook uses it to derive the project, boot image, and data-disk image names
unless explicit image names are supplied. If a VM has been migrated more
than once, the newest migration wins
unless you override with an explicit `ami_id`. "Newest" compares `migrated_at`
as a parsed instant, so timestamps with a non-`Z` UTC offset sort correctly,
and the catalog files and launch dialogs order rows the same way. New writes are fully
validated; the readers still tolerate rows written before the current schema.

## GCP Project Reconciliation

GCP migration and deployment catalog entries are scoped by `gcp_project`.
When more than one project is present, pass the project ID explicitly and scope
credentials with `--credentials-file` or standard Google credentials:

```bash
./bin/reconcile-catalog-gcp.py \
  --project-id my-project-a \
  --credentials-file /path/to/service-account.json \
  --dry-run
```

Entries for other projects are left untouched. When no `--credentials-file` is
given, the ambient default project from Application Default Credentials (gcloud
config, `GOOGLE_CLOUD_PROJECT`) is ignored; the run is refused only when the
credentials themselves carry a different project, as a service account key does.

## OpenStack Catalog Reconciliation

Reconcile OpenStack migration images and deployments against the live cloud
when resources may have been removed outside this repository. The script uses
resource UUIDs from both catalogs and removes the exact entries whose resources
are missing. A shared clone name or server ID does not remove any other row.
It never deletes cloud resources, but removed rows are not kept anywhere: if
your catalogs are committed to Git, its history holds the previous state;
otherwise copy the catalog files first. It also removes OpenStack entries already marked
`retired` by an older reconciliation run. Use `--dry-run` first:

```bash
./bin/reconcile-catalog-openstack.py \
  --region-name US-WEST-OR-1 \
  --project-id 9876543210123456 \
  --dry-run

./bin/reconcile-catalog-openstack.py \
  --region-name US-WEST-OR-1 \
  --project-id 9876543210123456
```

Only entries for the selected region are checked, because Glance, Nova, and
Cinder IDs are per region. Older rows with no recorded `openstack_region`
could belong to any region, where their resources would look missing from
here, so they are skipped: each is reported on a "skipping ... no
openstack_region recorded" line and is neither checked nor removed. Such a
row already marked `retired` is still removed, because that goes by status
rather than by a resource lookup. The `--force` threshold counts only the
rows that are checked.

One run reconciles one project: `--project-id` accepts a project name or ID
and is required whenever the
catalogs hold more than one OpenStack project, and entries for other projects
are left alone rather than treated as an error. Repeat per project, scoping
the standard `openstacksdk` environment or `--cloud <name>` auth to each
project in turn.

OpenStack authentication comes from the standard `openstacksdk` environment or
`clouds.yaml` configuration. Unlike the AWS and GCP reconcilers, which find
`~/.aws` or Application Default Credentials on their own, this script does not
read the encrypted `vaults/` files and has no default cloud, so it exits with
"missing required OpenStack environment variable(s)" unless you pick one of:

```bash
# 1. Name a clouds.yaml entry (~/.config/openstack/clouds.yaml)
./bin/reconcile-catalog-openstack.py --cloud my-cloud \
  --project-id my-project-a --region-name US-WEST-OR-1 --dry-run

# 2. Select it for the shell with OS_CLOUD
export OS_CLOUD=my-cloud

# 3. Source an OpenStack RC file (OS_AUTH_URL, OS_USERNAME, OS_PASSWORD,
#    OS_PROJECT_NAME or OS_PROJECT_ID, ...) downloaded from the dashboard
source ~/my-project-a-openrc.sh
./bin/reconcile-catalog-openstack.py \
  --project-id my-project-a --region-name US-WEST-OR-1 --dry-run
```

Each project needs its own `--cloud` entry or RC file, so source or name the one
matching `--project-id` before every run. `--region-name` defaults to the catalog's region
for that project; use `--cloud <name>`
for a named `clouds.yaml` entry and `--project-id` (name or ID) to select the project.
The authenticated project ID must match `--project-id` (string-compared, so
either form works), and it must have entries
in the requested region; other projects' entries are out of scope and kept.
API errors abort reconciliation; they are
not treated as missing resources. A migration entry is removed when any
recorded Glance image is missing. A deployment entry is removed when its
server or any recorded data volume is missing. When the server is gone but
pre-created network-identity ports from `openstack_port_ids` (or port-id
entries in `openstack_nics`) still exist, the dry-run output lists those
orphan ports so operators can delete them before reusing the MAC or fixed IP;
the reconciler never deletes cloud resources itself. If more than half of the
scoped entries are stale, the write is refused; inspect the dry run and add
`--force` only when that removal is intentional.

## Deployment Catalog

Each OpenStack launch uses a unique launch token in additional Cinder volume
names and pre-created network-identity port names. Set `launch_run_id`
explicitly when retrying the same launch operation only after confirming that
the prior server and its ports were removed; ports already attached to a
server cannot be reused, and their MACs remain reserved until the ports are
deleted.

Every successful AWS, OpenStack, or GCP launch appends one record to
`catalogs/deployment-catalog.json` via `bin/append-deployment-catalog.py`.
AWS records contain `vm_name`, `target_platform`, `ami_id`, the
`import_task_id` it came from, `instance_id`, `instance_type`, `aws_region`,
`vpc_subnet_id`, `status`, and `launched_at`. OpenStack records contain
`vm_name`, `target_platform`, `openstack_server_id`, the ordered image IDs,
project (key), optional project ID and region, flavor, network and
per-NIC `openstack_network_ids`, the port-backed `openstack_nics` entries,
explicit `openstack_port_ids` when ports were pre-created, attached
data-volume IDs, `status`, and `launched_at`. Pre-created ports are not
deleted automatically when a server is later deleted; remove them
explicitly when their MACs or fixed IPs must be reused.
`bin/reconcile-catalog-openstack.py` reports remaining port IDs as orphans
when it removes a deployment whose server is already gone. GCP records contain the instance name, project, zone, machine
type, boot image, attached data-disk names, status, and `launched_at`.
Deployment `status` is recorded as each cloud reports it, so its casing is
per platform: OpenStack `ACTIVE`, AWS `running`, GCP `RUNNING`. Only the
lowercase `retired` left by older reconciliation runs is shared. Compare `status` case-insensitively
when filtering across platforms.
Same accumulate-not-supersede reasoning as the migration catalog - the same
AMI or image can legitimately be launched more than once, so history is kept
rather than overwritten; only an exact rerun against the same target resource
identity replaces its own prior entry.
