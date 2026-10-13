# Vaults Required by the Playbooks

## Editing a Vault

`bin/aws-vault.sh`, `bin/gcp-vault.sh`, `bin/openstack-vault.sh`, and
`bin/vsphere-vault.sh` wrap `ansible-vault edit`/`create
--encrypt-vault-id=default` against the matching file below, creating it if it
doesn't exist yet:

```sh
bin/aws-vault.sh
```

Set `VAULT` to point at a different file (e.g. a per-environment vault):

```sh
VAULT=vaults/aws-staging.yml bin/aws-vault.sh
```

## AWS Vault

vaults/aws.yml

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

Select an account with `-e aws_target_project=<key-or-account-id>`. A
single-entry map is auto-selected. A legacy flat mapping containing
`aws_region`, `s3_bucket`, and `vmimport_role_name` remains supported.

## GCP Vault

vaults/gcp.yml

```yaml
gcp_projects:
  my-project:
    gcp_project: "my-project"
    gcs_bucket: "my-bucket"
    gcp_import_location: "us-central1"
    service_account_key: |
      {
        "type": "service_account",
        "project_id": "my-project",
        "client_email": "you@my-project.iam.gserviceaccount.com"
      }
```

Select a project with `-e gcp_target_project=<key-or-project-id>`. A
single-entry map is auto-selected. A legacy flat mapping containing
`gcp_project`, `gcs_bucket`, `gcp_import_location`, and `service_account_key`
remains supported.

### `gcp_import_location`

The region the Migrate to Virtual Machines _import job_ runs in - the value
passed to `gcloud compute migration image-imports --location`. It is easy to
misread this as a storage setting, so, concretely, it does **not** control:

- **Where `gcs_bucket` lives.** A bucket's location is fixed when the bucket
  is created and is not settable from here. The import reads the staged QCOW2
  across regions perfectly well when the two differ.
- **Where the finished image lives.** A GCE Compute image is a _global_
  resource, so the imported image is equally usable from every region no
  matter which one converted it.

So this is purely a choice of which region does the conversion work, and it
can be changed freely - including to route around a regional outage:

```sh
./bin/migrate-gcp-vm.sh -e vm_name=my-vm -e gcp_import_location=us-east1
```

On 2026-08-12 the import service in `us-central1` failed four consecutive
times at the `loadingSourceFiles` step with `code 13`, "Internal migration
service error", while the byte-identical staged object imported cleanly in
`us-east1`. If an import fails with an internal error and no useful detail,
retrying in another region is the fastest way to tell a bad artifact apart
from a bad region.

## OpenStack Vault

vaults/openstack.yml

```txt
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

# A legacy single-project openstack_auth mapping remains supported.
```

A vault may contain either the legacy single `openstack_auth` mapping or an
`openstack_projects` mapping. Select a project with
`-e openstack_target_project=<vault-key-or-project-name-or-project-id>`; a
single-entry map is selected automatically. Quote numeric project names and IDs.
`region_name` should be set for multi-region clouds and is recorded on the
image catalog entry.

The catalog reconciliation script is a separate CLI workflow and uses the
OpenStack SDK environment variables or a `clouds.yaml` entry, rather than
reading `vaults/openstack.yml` directly. See
[OpenStack Catalog Reconciliation](../README.md#openstack-catalog-reconciliation)
for the required `OS_*` variables and the `--cloud`/`OS_CLOUD` alternatives.

## vSphere Vault

`vaults/vsphere.yml`

```yaml
vsphere_projects:
  example:
    vcenter_hostname: "my-vcenter"
    vcenter_username: "my-admin-user"
    vcenter_password: "my-admin-password"
    datacenter: "Datacenter"
    validate_certs: true
  other-site:
    vcenter_hostname: "other-vcenter"
    vcenter_username: "my-admin-user"
    vcenter_password: "other-password"
    datacenter: "Other-Datacenter"
    validate_certs: true

# A legacy flat vcenter_hostname/vcenter_username/vcenter_password mapping
# remains supported.
```

Select a vCenter with `-e vsphere_target_vcenter=<key-or-hostname>`. A
single-entry map is auto-selected. The selected vCenter key is recorded in
image-catalog scope so same-named content libraries remain distinct.
