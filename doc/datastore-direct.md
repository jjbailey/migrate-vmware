# Datastore-Direct OpenStack Migration

<!-- markdownlint-disable MD013 -->

`playbooks/migrate-openstack-vm-vddk.yml` is a second way to migrate a running
vSphere VM to OpenStack. It does not clone the VM and does not export an OVF/OVA
package. It takes a quiesced snapshot of the source VM, streams each frozen disk
straight from the datastore, converts it to QCOW2, and uploads it to Glance. The
existing `migrate-openstack-vm.yml` is unchanged; the two methods share the
catalog format, so launch, lookup, and reconcile work on either.

## How it works

```text
quiesced snapshot of the source VM          (no clone)
  → build the disk manifest from `govc vm.info` taken BEFORE the snapshot
  → per disk, one conversion ahead of the upload:
      nbdkit -r  vddk | curl   → read-only NBD export on a Unix socket
        → qemu-img convert -f raw -O qcow2  disks/disk-N.qcow2
        → openstack.cloud.image  (QCOW2 → Glance)
  → catalog entry (same schema as the OVF method)
  → snapshot removed (only after success)
```

Disk 0, the boot disk, is the disk named first in the VM's configured boot order
(`config.bootOptions.bootOrder`); when the VM sets none, it is the first disk by
controller and unit, the firmware default. The manifest records which as
`boot_order_source`.

Two backends serve the disk to `qemu-img`. Both read the snapshot's frozen data,
so the result is a point-in-time image; `quiesce_snapshot` (default true) makes it
application-consistent where the guest supports it. The source VM is never
powered off.

| Backend                         | How it reads                                                                                                                            | Needs                                 | Limits                                                                                      |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------- | ------------------------------------------------------------------------------------------- |
| `vddk` (default when available) | VixDiskLib through nbdkit's `vddk` plugin: vCenter login, ticket, NFC over TLS to the ESXi host, disks opened at the migration snapshot | nbdkit `vddk` plugin and a VixDiskLib | none of the curl limits                                                                     |
| `curl` (fallback)               | each `-flat.vmdk` file from vCenter's `/folder` endpoint with HTTP Range requests, through nbdkit's `curl` plugin                       | nothing beyond nbdkit                 | flat disks only, no existing snapshots; on VMFS the VM must be powered off; reads zeros too |

`openstack_vddk_backend` (wrapper: `BACKEND`) selects `auto` (the default: `vddk`
when the plugin and library load, else `curl`, with a message saying which of the
two is missing), `vddk`,
or `curl`. The manifest records the backend used as `transfer_backend`, and a
resume reads the disks with that same backend (see the resume notes below).

Measured on a running two-disk Debian VM (38 GB thin boot disk on an NFS
datastore): the `vddk` backend read the boot disk in about 4 minutes
(~150 MiB/s), the `curl` backend in about 15 (~40 MiB/s), and the two results
were byte-for-byte identical.

A QCOW2 file is still staged on the controller for each disk in flight (the
Glance module uploads from a file). Peak staging is one or two QCOW2 files, not a
full copy of the VM.

### Compression for slow upload links

`COMPRESS=true` (`-e openstack_convert_compress=true`, off by default) adds
`qemu-img convert -c`: clusters are compressed as the QCOW2 is written, in the
same single pass, so both the staging file and the upload shrink. Anything that
reads QCOW2 decompresses transparently; Glance stores the image as uploaded. On
the test VM the boot disk shrank from 2.62 GB to 1.03 GB, the conversion took
about as long, the whole migration over a slow uplink dropped from about 35 to
about 11 minutes, and an OVH instance booted from the compressed images. The
cost is controller CPU, so leave it off on a fast link.

## Usage

```bash
VM_NAME=my-vm VSPHERE_TARGET_VCENTER=my-vcenter ./bin/migrate-openstack-vm-vddk.sh
BACKEND=curl VM_NAME=my-vm ./bin/migrate-openstack-vm-vddk.sh   # force a backend
./bin/migrate-openstack-vddk-dialog.sh                           # guided front end (asks about compression)
```

The wrapper takes the same environment variables as `bin/migrate-openstack-vm.sh`
(`VM_NAME`, `VSPHERE_TARGET_VCENTER`, `OS_PROJECT_NAME`, `QUIESCE_SNAPSHOT`,
`VALIDATE_CERTS`, `ARTIFACT_ROOT`), plus `BACKEND`, `COMPRESS`, `REQUIRE_CBT`
(see below), and `VCENTER_CA_BUNDLE`.
Any Ansible arguments after the wrapper's own are passed through. Other playbook
settings: `openstack_vddk_libdir` (VixDiskLib directory; empty uses the plugin's
default), `openstack_vddk_transports` (default `nbdssl`), and
`openstack_vddk_thumbprint` (empty reads the vCenter certificate in preflight;
with `validate_certs` true the certificate must verify against this host's CAs
first, because VixDiskLib trusts whatever thumbprint it is given).
`openstack_vddk_ca_bundle` (wrapper: `VCENTER_CA_BUNDLE`) names a PEM file
holding the CA that issued the vCenter certificate, for a vCenter this host does
not already trust; the thumbprint check verifies against it and the curl backend
gets it as `cainfo=`, which needs an nbdkit curl plugin that accepts it
(preflight checks).

Resume a failed run with `-e resume_run_id=<id>` (printed on failure). The
snapshot is the resume checkpoint, so a failed run **keeps it**; a retained
snapshot keeps growing as the VM writes, so resume or discard promptly. To give
up on a failed run, discard it with `-e discard_run_id=<id>` (wrapper:
`DISCARD_RUN_ID=<id>`, with the same `VM_NAME`): that removes the run's snapshot,
its run directory, and any images it uploaded, including a partial upload, and
migrates nothing. (Passing `-e discard_failed_artifacts=true` to a new run does
not touch an earlier one.)

A resume reads the disks the way the failed run did, using the
`transfer_backend` its manifest records; `auto` follows the recorded value
rather than choosing again. Asking for the other backend explicitly fails, as
does a resume of a `vddk` run when the `vddk` plugin or library no longer
loads, because falling back to `curl` could read stale base disk files; the
message points to resuming with the recorded backend or discarding the run. A
manifest from before `transfer_backend` was recorded only draws a warning, and
the backend is chosen as for a new run. Whenever a resume uses `curl`, the
`curl` checks run again against the recorded disks, so a VM recorded with a
snapshot chain fails instead of being read from its base files.

A run that fails after taking its snapshot but before writing its manifest
cannot be resumed at all; the failure message says to discard it with
`-e discard_run_id=<id>` and start again. Do that promptly: the snapshot's
delta keeps growing until it is removed.

If a run fails while a disk is being uploaded, the next disk's background
stream and conversion are stopped before cleanup, so nothing keeps reading the
source or writing the staging directory while a resume or discard runs. If
`ansible-playbook` itself was killed and never reached that cleanup, the
resume or discard stops the streams the killed attempt left running before it
does anything else. Only an nbdkit still serving that run's own socket is
signalled, so a stale pidfile cannot stop an unrelated process.

Each disk's nbdkit log is kept in the run directory as `nbdkit-disk-<N>.log`,
and a failed conversion quotes it, so the failure says why the disk could not
be read (an untrusted certificate, a wrong URL, a refused login).

## Requirements

- On the control host: a Linux host (the stream bookkeeping reads `/proc` to
  confirm a pid still belongs to this run's socket, and relies on POSIX
  signals), `nbdkit`, `qemu-img`, `govc`, and the Ansible collections in
  `requirements.yml`. No `ovftool`.
- For the `vddk` backend: nbdkit's `vddk` plugin and a VixDiskLib (below).
- vCenter credentials from `vaults/vsphere.yml`, as for the other methods. The
  password is handed to nbdkit through a mode-0600 file, never on a command line;
  that file is per disk. The conversion shell removes it when the conversion
  ends; if the controller is killed first, the resume or discard that stops the
  leftover stream removes it, and cleanup removes the socket directory.
- The playbook refuses, before creating anything:
  - independent-persistent or independent-nonpersistent disks (a snapshot does
    not freeze them),
  - shared (multi-writer) disks, raw device mappings, and encrypted disks,
  - with the `curl` backend only: a VM that already has snapshots, and a disk
    file vCenter will not serve. On VMFS, ESXi locks a powered-on VM's
    `-flat.vmdk`, so `/folder` answers HTTP 500; power the VM off or use the
    `vddk` backend. NFS datastores serve a running VM's disks.

### Installing the `vddk` backend

nbdkit's `vddk` plugin loads VixDiskLib at run time from
`<libdir>/lib64/libvixDiskLib.so.8`. Ubuntu does not package the plugin; it
builds from nbdkit's source without VixDiskLib present. Use the source of the
nbdkit version already installed:

```bash
v=$(nbdkit --version | awk '{print $2}')        # e.g. 1.36.3
curl -fsSLO "https://download.libguestfs.org/nbdkit/${v%.*}-stable/nbdkit-$v.tar.gz"
curl -fsSLO "https://download.libguestfs.org/nbdkit/${v%.*}-stable/nbdkit-$v.tar.gz.sig"
gpg --verify "nbdkit-$v.tar.gz.sig" "nbdkit-$v.tar.gz"   # key F7774FB1AD074A7E8C8767EA91738F73E1B768A0
tar -xzf "nbdkit-$v.tar.gz" && cd "nbdkit-$v"
./configure --disable-perl --disable-python --disable-ocaml --disable-rust \
    --disable-ruby --disable-tcl --disable-lua --disable-golang
for d in common/include common/replacements common/utils common/bitmap plugins/vddk ; do
    make -C "$d"
done
sudo install -m 0755 plugins/vddk/.libs/nbdkit-vddk-plugin.so "$(pkg-config --variable=plugindir nbdkit 2>/dev/null || echo /usr/lib/x86_64-linux-gnu/nbdkit/plugins)/"
```

Then lay out the library where the plugin looks by default
(`nbdkit vddk --dump-plugin` prints `vddk_default_libdir`), for example:

```bash
sudo mkdir -p /usr/local/lib/vmware-vix-disklib/lib64
sudo ln -sfn /usr/local/lib/libvixDiskLib.so.8.0.3 /usr/local/lib/vmware-vix-disklib/lib64/libvixDiskLib.so.8
nbdkit vddk --dump-plugin | grep vddk_library_version     # proves both load
```

The library tested is `libvixDiskLib.so.8.0.3` from
[OpenVDDK](https://openvddk.com/), an independent, read-only replacement for
VMware's disk library (HotAdd, NBDSSL, and NBD transports; see its site for
availability). VMware's own VDDK, where you are licensed for it, works the same
way through `libdir`.

With the OpenVDDK build that supports `OpenVDDK.nbd.sessions`, each disk uses
**two parallel NFC sessions** over NBD/NBDSSL rather than the library's default
of one. The conversion task writes `OpenVDDK.nbd.sessions = "2"` to a private
per-disk configuration file and passes it to nbdkit's VDDK plugin with `config=`.
This requires the new OpenVDDK library to be installed in the selected `libdir`;
the playbook does not download or replace it. VMware's library does not implement
this OpenVDDK-specific setting. The curl fallback and local-file tests are
unchanged.

## CBT (Changed Block Tracking)

A full copy of a snapshot does not need CBT, so it is **off by default**.
`-e openstack_vddk_require_cbt=true` (or `REQUIRE_CBT=true`) makes the playbook
insist on it. `bin/enable-cbt.py` (pyVmomi) can enable it ahead of time: it
reconfigures the VM without a power cycle and is idempotent. It needs
`VCENTER_HOSTNAME`, `VCENTER_USERNAME`, and `VCENTER_PASSWORD`, and `--datacenter`,
`--vm`, and optionally `--insecure`.

Setting `ctkEnabled` in the VM's ExtraConfig (for example with `govc vm.change
-e`) does **not** work: vCenter leaves `changeTrackingEnabled` false. govc has no
`-cbt` option. CBT is only useful for incremental migrations, which are not
implemented: they would also need the snapshot's change ID saved in the catalog.

## Files

| File                                                                                                               | Role                                                                                   |
| ------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------- |
| `playbooks/migrate-openstack-vm-vddk.yml`                                                                          | entry playbook and settings                                                            |
| `bin/migrate-openstack-vm-vddk.sh`, `bin/migrate-openstack-vddk-dialog.sh`                                         | wrapper and guided front end                                                           |
| `bin/inspect-vm.py`                                                                                                | builds the disk manifest from `govc vm.info -json`; refuses unsupported disks          |
| `bin/enable-cbt.py`                                                                                                | optional CBT helper                                                                    |
| `playbooks/tasks/migration/common/prepare-source-manifest.yml`                                                     | snapshot and manifest                                                                  |
| `playbooks/tasks/migration/common/snapshot-ref-vddk.yml`                                                           | the snapshot's reference for VixDiskLib                                                |
| `playbooks/tasks/migration/common/{preflight,resume,cleanup}-vddk.yml`                                             | copies of the OVF method's tasks without the OVF/clone assumptions                     |
| `playbooks/tasks/migration/common/check-curl-disks-vddk.yml`                                                       | the `curl` backend's disk checks, run before the snapshot and again on a `curl` resume |
| `playbooks/tasks/migration/openstack/{initialize,preflight,import,import-disk,convert-disk,stop-streams}-vddk.yml` | backend choice, per-disk stream, convert, and upload; stopping streams after a failure |

The `-vddk` task files are deliberate copies of their OVF counterparts, so each
method can change without touching the other. A fix to the shared upload or
overlap logic should be considered for both. The tasks that need no change
(`check-image`, `record-image`, `build-catalog-entry`, `append-catalog`,
`load-manifest`, `validate-disk-order`, `openstack/cleanup`) are imported as is.

The catalog entry keeps a `clone_name` field because the catalog and its readers
key on it; here it is only an identifier (`<vm>-openstack-vddk-<run id>`), and no
such VM exists.

## Tested

On a running two-disk Debian VM (38 GB thin boot disk, 1 GB data disk) on an NFS
datastore:

- both backends: snapshot, stream, convert, upload, catalog entry, and snapshot
  and temporary-file cleanup; the two backends' boot-disk images were
  byte-for-byte identical;
- instances launched on OVH booted with both disks attached, from the `curl`
  backend's images and from the `vddk` backend's compressed images.

Not yet tested: resume after a failure, VMs that already have snapshots (`vddk`
backend), and VMFS, vSAN, or vVol datastores. VixDiskLib's allocated-block query
is used, but the NFS datastore reported every block allocated, so zeros were
still read.

**Resume is therefore experimental.** The retention, `resume_run_id`, and
`discard_run_id` paths are implemented and the backend used by a run is pinned
in its manifest so a resume cannot silently read a disk differently, but a
resumed datastore-direct run has not been exercised against a real vCenter.
Prefer `discard_run_id` plus a fresh run until it has been.
