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
  newer releases may also work but are not part of the validated toolchain.
  Download the tested **OVF Tool for Linux Zip** from Broadcom's [OVF Tool 5.1.0 download page](https://developer.broadcom.com/tools/open-virtualization-format-ovf-tool/5.1.0)
  after signing in and accepting Broadcom's license terms. VMware/Broadcom's license does not permit
  redistribution, so the ZIP is **not included in this repo**.

  Install the tool on the control host according to Broadcom's instructions
  and verify that `ovftool` is available on `PATH`:

  ```bash
  ovftool --version
  ```

## Coming soon

## doc/

- [workflow.md](doc/workflow.md) - Describes the per-target migration run
  (snapshot, clone, OVA export, manifest generation), the migration catalog,
  and artifact retention/cleanup behavior on success and failure. Includes a
  Mermaid flowchart of the AWS/OpenStack/GCP run paths.
- [workflow.svg](doc/workflow.svg) - Rendered version of the flowchart in
  `workflow.md`.
- [migrate_vmware_presentation.pptx](doc/migrate_vmware_presentation.pptx) -
  Slide deck presenting the migration workflow.
- [migrate_vmware_presentation.mp4](doc/migrate_vmware_presentation.mp4) -
  Recorded narration of the presentation deck.
