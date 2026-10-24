#!/usr/bin/env python3
# tests/test_catalogs.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Focused tests for catalog validation, OVA inspection, and reconcile guards."""

import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import catalog_common  # noqa: E402

def load_script(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

migration = load_script("append-migration-catalog.py", "append_migration_catalog")
deployment = load_script("append-deployment-catalog.py", "append_deployment_catalog")
inspect_ova = load_script("inspect-ova.py", "inspect_ova")
reconcile = load_script("reconcile-catalog-openstack.py", "reconcile_openstack")
reconcile_aws = load_script("reconcile-catalog-aws.py", "reconcile_aws")

class CatalogValidationTests(unittest.TestCase):
    def migration_entry(self):
        return {
            "vm_name": "vm-a", "source_vcenter": "vc.example",
            "source_datacenter": "dc-a", "clone_name": "vm-a-run-1",
            "snapshot_name": "snap-1", "target_platform": "openstack",
            "artifact_type": "ova", "artifact_manifest": {
                "manifest_version": 1, "disk_count": 2,
                "disks": [{"index": 0, "boot_candidate": True, "capacity_bytes": 10},
                          {"index": 1, "boot_candidate": False, "capacity_bytes": 20}],
            },
            "status": "completed", "migrated_at": "2026-09-14T12:00:00Z",
            "openstack_image_ids": ["image-boot", "image-data"],
            "openstack_image_names": ["vm-a-disk-0", "vm-a-disk-1"],
            "openstack_project": "project-a",
        }

    def deployment_entry(self):
        return {
            "vm_name": "vm-a", "target_platform": "openstack", "status": "ACTIVE",
            "launched_at": "2026-09-14T12:00:00Z", "openstack_server_id": "server-a",
            "openstack_image_ids": ["image-boot", "image-data"], "openstack_project": "project-a",
            "openstack_flavor": "small", "openstack_network": "net-a",
            "openstack_data_volume_ids": ["volume-data"],
        }

    def test_migration_validator_enforces_disk_and_image_order(self):
        entry = self.migration_entry()
        entry["openstack_image_names"] = ["vm-a-disk-1", "vm-a-disk-0"]
        with self.assertRaisesRegex(ValueError, "out of manifest disk order"):
            migration.validate(entry)
        entry = self.migration_entry()
        entry["artifact_manifest"]["disks"].reverse()
        with self.assertRaisesRegex(ValueError, "must carry index"):
            migration.validate(entry)
        entry = self.migration_entry()
        entry["artifact_manifest"]["disks"][1]["boot_candidate"] = True
        with self.assertRaisesRegex(ValueError, "only disk index 0"):
            migration.validate(entry)

    def test_gcp_validator_enforces_image_order(self):
        entry = self.migration_entry()
        entry.update({
            "target_platform": "gcp", "artifact_type": "vmdk-disk-set",
            "gcp_project": "p", "gcp_import_location": "us", "gcs_bucket": "b",
            "gcs_prefix": "x", "gcp_boot_image_name": "vm-a-disk-0",
            "gcp_boot_image_selflink": "https://x/images/vm-a-disk-0",
            "gcp_data_image_names": ["vm-a-disk-1"],
            "gcp_data_image_selflinks": ["https://x/images/vm-a-disk-1"],
        })
        migration.validate(entry)
        entry["gcp_boot_image_name"] = "vm-a-disk-1"
        entry["gcp_data_image_names"] = ["vm-a-disk-0"]
        with self.assertRaisesRegex(ValueError, "out of manifest disk order"):
            migration.validate(entry)

    def test_migration_validator_accepts_matching_multi_disk_entry(self):
        migration.validate(self.migration_entry())

    def test_migration_validator_rejects_image_count_mismatch(self):
        entry = self.migration_entry()
        entry["openstack_image_ids"].pop()
        with self.assertRaisesRegex(ValueError, "image_ids count"):
            migration.validate(entry)

    def test_deployment_validator_accepts_openstack_entry(self):
        deployment.validate(self.deployment_entry())

    def test_deployment_validator_accepts_port_backed_entry(self):
        entry = self.deployment_entry()
        entry["openstack_network"] = ""
        entry["openstack_network_id"] = ""
        entry["openstack_network_ids"] = ["net-uuid-a"]
        entry["openstack_nics"] = [{"port-id": "port-a"}]
        entry["openstack_port_ids"] = ["port-a"]
        deployment.validate(entry)

    def test_deployment_validator_rejects_missing_network_identity(self):
        entry = self.deployment_entry()
        entry["openstack_network"] = ""
        entry["openstack_network_id"] = ""
        entry["openstack_nics"] = []
        with self.assertRaisesRegex(ValueError, "network name/id"):
            deployment.validate(entry)

    def test_deployment_validator_rejects_empty_image_list(self):
        entry = self.deployment_entry()
        entry["openstack_image_ids"] = []
        with self.assertRaisesRegex(ValueError, "image_ids"):
            deployment.validate(entry)

    def test_describe_deployment_reports_orphan_ports(self):
        entry = self.deployment_entry()
        message = reconcile.describe_deployment(
            entry, True, [], ["port-orphan"]
        )
        self.assertIn("orphan network-identity port(s)", message)
        self.assertIn("port-orphan", message)

class OvaInspectionTests(unittest.TestCase):
    def test_capacity_bytes_supports_binary_units_and_rejects_fraction(self):
        self.assertEqual(inspect_ova.capacity_bytes("2", "byte * 2^30"), (2 ** 30) * 2)
        with self.assertRaisesRegex(ValueError, "non-integral"):
            inspect_ova.capacity_bytes("0.5", "byte")

    def test_inspect_ova_orders_attached_disks(self):
        ovf = """<?xml version="1.0"?>
        <Envelope xmlns="urn:schemas-dmtf-org:ovf" xmlns:ovf="urn:schemas-dmtf-org:ovf">
          <References>
            <File ovf:id="f2" ovf:href="data.vmdk" ovf:size="20" />
            <File ovf:id="f1" ovf:href="boot.vmdk" ovf:size="10" />
          </References>
          <DiskSection>
            <Disk ovf:diskId="disk-data" ovf:fileRef="f2" ovf:capacity="20" ovf:capacityAllocationUnits="byte" />
            <Disk ovf:diskId="disk-boot" ovf:fileRef="f1" ovf:capacity="10" ovf:capacityAllocationUnits="byte" />
          </DiskSection>
          <VirtualSystem>
            <Item><rasd:ResourceType xmlns:rasd="urn:schemas-dmtf-org:ovf">17</rasd:ResourceType><rasd:HostResource xmlns:rasd="urn:schemas-dmtf-org:ovf">/disk/disk-boot</rasd:HostResource><rasd:InstanceID xmlns:rasd="urn:schemas-dmtf-org:ovf">10</rasd:InstanceID><rasd:AddressOnParent xmlns:rasd="urn:schemas-dmtf-org:ovf">0</rasd:AddressOnParent></Item>
            <Item><rasd:ResourceType xmlns:rasd="urn:schemas-dmtf-org:ovf">17</rasd:ResourceType><rasd:HostResource xmlns:rasd="urn:schemas-dmtf-org:ovf">/disk/disk-data</rasd:HostResource><rasd:InstanceID xmlns:rasd="urn:schemas-dmtf-org:ovf">11</rasd:InstanceID><rasd:AddressOnParent xmlns:rasd="urn:schemas-dmtf-org:ovf">1</rasd:AddressOnParent></Item>
          </VirtualSystem>
        </Envelope>"""
        with tempfile.TemporaryDirectory() as directory:
            ova = Path(directory) / "vm.ova"
            with tarfile.open(ova, "w") as archive:
                data = ovf.encode()
                info = tarfile.TarInfo("vm.ovf")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            manifest = inspect_ova.inspect_ova(ova)
        self.assertEqual(manifest["disk_count"], 2)
        self.assertEqual([disk["disk_id"] for disk in manifest["disks"]], ["disk-boot", "disk-data"])
        self.assertTrue(manifest["disks"][0]["boot_candidate"])

    def test_inspect_ovf_validates_standalone_disk(self):
        ovf = """<?xml version="1.0"?>
        <Envelope xmlns="urn:schemas-dmtf-org:ovf" xmlns:ovf="urn:schemas-dmtf-org:ovf">
          <References><File ovf:id="f1" ovf:href="disk.vmdk" ovf:size="4" /></References>
          <DiskSection><Disk ovf:diskId="disk-boot" ovf:fileRef="f1" ovf:capacity="4" ovf:capacityAllocationUnits="byte" /></DiskSection>
        </Envelope>"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "disk.vmdk").write_bytes(b"test")
            descriptor = root / "export.ovf"
            descriptor.write_text(ovf)
            manifest = inspect_ova.inspect_ovf(descriptor)
        self.assertEqual(manifest["artifact_type"], "ovf")
        self.assertEqual(manifest["disks"][0]["file_size_bytes"], 4)
        self.assertEqual(manifest["disks"][0]["path"], "disk.vmdk")

    def test_inspect_ovf_allows_missing_file_size(self):
        ovf = """<?xml version="1.0"?>
        <Envelope xmlns="urn:schemas-dmtf-org:ovf" xmlns:ovf="urn:schemas-dmtf-org:ovf">
          <References><File ovf:id="f1" ovf:href="disk.vmdk" /></References>
          <DiskSection><Disk ovf:diskId="disk-boot" ovf:fileRef="f1" ovf:capacity="4" ovf:capacityAllocationUnits="byte" /></DiskSection>
        </Envelope>"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "disk.vmdk").write_bytes(b"test")
            descriptor = root / "export.ovf"
            descriptor.write_text(ovf)
            manifest = inspect_ova.inspect_ovf(descriptor)
        self.assertIsNone(manifest["disks"][0]["file_size_bytes"])

    def test_inspect_ovf_rejects_file_compression_metadata(self):
        ovf = """<?xml version="1.0"?>
        <Envelope xmlns="urn:schemas-dmtf-org:ovf" xmlns:ovf="urn:schemas-dmtf-org:ovf">
          <References><File ovf:id="f1" ovf:href="disk.vmdk" ovf:size="4" ovf:compression="gzip" /></References>
          <DiskSection><Disk ovf:diskId="disk-boot" ovf:fileRef="f1" ovf:capacity="4" ovf:capacityAllocationUnits="byte" /></DiskSection>
        </Envelope>"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "disk.vmdk").write_bytes(b"test")
            descriptor = root / "export.ovf"
            descriptor.write_text(ovf)
            with self.assertRaisesRegex(ValueError, "compressed OVF disk"):
                inspect_ova.inspect_ovf(descriptor)

    def test_inspect_ovf_rejects_gzip_disk_reference(self):
        ovf = """<?xml version="1.0"?>
        <Envelope xmlns="urn:schemas-dmtf-org:ovf" xmlns:ovf="urn:schemas-dmtf-org:ovf">
          <References><File ovf:id="f1" ovf:href="disk.vmdk.gz" ovf:size="4" /></References>
          <DiskSection><Disk ovf:diskId="disk-boot" ovf:fileRef="f1" ovf:capacity="4" ovf:capacityAllocationUnits="byte" /></DiskSection>
        </Envelope>"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "disk.vmdk.gz").write_bytes(b"test")
            descriptor = root / "export.ovf"
            descriptor.write_text(ovf)
            with self.assertRaisesRegex(ValueError, "gzip-compressed"):
                inspect_ova.inspect_ovf(descriptor)

class ReconcileGuardTests(unittest.TestCase):
    def catalog(self):
        return {"migrations": [
            {"target_platform": "openstack", "openstack_project": "project-a", "openstack_region": "region-a"},
            {"target_platform": "openstack", "openstack_project": "project-b", "openstack_region": "region-a"},
        ], "deployments": []}

    def test_scope_accepts_other_projects_but_returns_authenticated_scope(self):
        connection = SimpleNamespace(current_project_id="project-a", current_project=SimpleNamespace(name="project-a"))
        identities = reconcile.validate_scope(connection, self.catalog(), {"migrations": [], "deployments": []}, "project-a", "region-a")
        self.assertEqual(identities, {"project-a"})

    def test_scope_rejects_authenticated_project_mismatch(self):
        connection = SimpleNamespace(current_project_id="project-b", current_project=SimpleNamespace(name="project-b"))
        with self.assertRaisesRegex(ValueError, "does not match"):
            reconcile.validate_scope(connection, self.catalog(), {"migrations": [], "deployments": []}, "project-a", "region-a")

class CatalogRemovalTests(unittest.TestCase):
    """Removal must never touch a row the caller did not identify."""

    def write(self, rows):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"schema_version": 1, "migrations": rows, "deployments": []}, handle)
        handle.close()
        return Path(handle.name)

    def rows(self):
        return [
            {"vm_name": "keep-me", "target_platform": "aws",
             "status": "completed", "import_task_id": "import-ami-111"},
            # Pre-schema row kept for provenance: no import_task_id.
            {"vm_name": "innocent-legacy", "target_platform": "aws",
             "status": "completed"},
            {"vm_name": "retire-me", "target_platform": "aws",
             "status": "retired"},
        ]

    def test_retired_row_without_identity_removes_only_itself(self):
        path = self.write(self.rows())
        try:
            retired = [{"vm_name": "retire-me", "target_platform": "aws",
                        "status": "retired"}]
            removed = catalog_common.remove_migration_entries(
                path, [], retired, "aws", "import_task_id", None)
            remaining = [row["vm_name"] for row in json.loads(path.read_text())["migrations"]]
            # The identified retired row goes. A missing import_task_id must
            # not also delete the other pre-schema row.
            self.assertEqual(removed, 1)
            self.assertEqual(sorted(remaining), ["innocent-legacy", "keep-me"])
        finally:
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".lock").unlink(missing_ok=True)

    def test_removals_never_exceed_the_rows_requested(self):
        # The requested row has look-alikes that share its vm_name, its
        # import_task_id, or both, but differ elsewhere. Only the exact row
        # may go, so the count removed equals the count requested.
        target = {"vm_name": "retire-me", "target_platform": "aws",
                  "status": "retired", "import_task_id": "import-ami-222",
                  "aws_region": "us-west-2"}
        lookalikes = [
            dict(target, aws_region="us-east-1"),
            dict(target, vm_name="other-vm"),
            dict(target, status="completed"),
        ]
        rows = self.rows() + [target] + lookalikes
        path = self.write(rows)
        try:
            removed = catalog_common.remove_migration_entries(
                path, [], [dict(target)], "aws", "import_task_id", None)
            remaining = json.loads(path.read_text())["migrations"]
            self.assertEqual(removed, 1)
            self.assertEqual(len(remaining), len(rows) - 1)
            self.assertNotIn(target, remaining)
            for row in lookalikes:
                self.assertIn(row, remaining)

            # A requested row that is no longer present removes nothing and
            # leaves the file alone.
            before = path.read_text()
            removed = catalog_common.remove_migration_entries(
                path, [], [dict(target)], "aws", "import_task_id", None)
            self.assertEqual(removed, 0)
            self.assertEqual(path.read_text(), before)
        finally:
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".lock").unlink(missing_ok=True)

    def test_identified_row_is_still_removed(self):
        rows = self.rows()
        rows[2]["import_task_id"] = "import-ami-222"
        path = self.write(rows)
        try:
            retired = [dict(rows[2])]
            removed = catalog_common.remove_migration_entries(
                path, [], retired, "aws", "import_task_id", None)
            remaining = [row["vm_name"] for row in json.loads(path.read_text())["migrations"]]
            self.assertEqual(removed, 1)
            self.assertEqual(sorted(remaining), ["innocent-legacy", "keep-me"])
        finally:
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".lock").unlink(missing_ok=True)

class SharedIdentityRemovalTests(unittest.TestCase):
    """A reused cloud ID must not take a healthy row with it."""

    def write(self, rows, key="migrations"):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8")
        payload = {"schema_version": 1, "migrations": [], "deployments": []}
        payload[key] = rows
        json.dump(payload, handle)
        handle.close()
        return Path(handle.name)

    def test_shared_clone_name_removes_only_the_stale_row(self):
        stale = {
            "vm_name": "vm-a", "target_platform": "openstack",
            "status": "completed", "clone_name": "shared-clone",
            "openstack_region": "region-a", "migrated_at": "2026-09-01T00:00:00Z",
        }
        healthy = {
            "vm_name": "vm-b", "target_platform": "openstack",
            "status": "completed", "clone_name": "shared-clone",
            "openstack_region": "region-b", "migrated_at": "2026-09-02T00:00:00Z",
        }
        path = self.write([stale, healthy])
        try:
            removed = catalog_common.remove_migration_entries(
                path, [(stale, ["image-a"])], [], "openstack", "clone_name",
                "region-a", {"project-a"})
            remaining = json.loads(path.read_text())["migrations"]
            self.assertEqual(removed, 1)
            self.assertEqual([row["vm_name"] for row in remaining], ["vm-b"])
        finally:
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".lock").unlink(missing_ok=True)

    def test_stale_deployment_does_not_remove_retired_twin(self):
        stale = {
            "vm_name": "vm-a", "target_platform": "openstack",
            "status": "ACTIVE", "openstack_server_id": "server-1",
        }
        retired = {
            "vm_name": "vm-a", "target_platform": "openstack",
            "status": "retired", "openstack_server_id": "server-1",
            "launched_at": "2026-08-01T00:00:00Z",
        }
        path = self.write([stale, retired], key="deployments")
        try:
            removed = catalog_common.remove_deployment_entries(
                path, [(stale, True, [], [])], [], "openstack",
                "openstack_server_id", "region-a", {"project-a"})
            remaining = json.loads(path.read_text())["deployments"]
            self.assertEqual(removed, 1)
            self.assertEqual(remaining[0]["status"], "retired")
        finally:
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".lock").unlink(missing_ok=True)

class OpenStackLookupFilterTests(unittest.TestCase):
    def test_region_filter_skips_another_region_but_keeps_legacy_rows(self):
        lookup = load_script(
            "lookup-migration-openstack.py", "lookup_migration_openstack")
        args = SimpleNamespace(project="project-a", region="region-a")
        accept = lookup.project_filter(args)
        self.assertTrue(accept({
            "openstack_project": "project-a", "openstack_region": "region-a"}))
        self.assertFalse(accept({
            "openstack_project": "project-a", "openstack_region": "region-b"}))
        self.assertTrue(accept({"openstack_project": "project-a"}))
        self.assertFalse(accept({
            "openstack_project": "project-b", "openstack_region": "region-a"}))

class RegressionTests(unittest.TestCase):
    def test_openstack_reconcile_skips_other_regions(self):
        entry = {"target_platform": "openstack", "status": "completed",
                 "clone_name": "c1", "openstack_project": "p",
                 "openstack_region": "RegionTwo", "openstack_image_ids": ["img"]}
        connection = SimpleNamespace(get_image=lambda image_id: None)
        catalogs = ({"migrations": [entry]}, {"deployments": []})
        stale, _ = reconcile.find_missing_resources(
            connection, *catalogs, {"p"}, "RegionOne")
        self.assertEqual(stale, [])
        stale, _ = reconcile.find_missing_resources(
            connection, *catalogs, {"p"}, "RegionTwo")
        self.assertEqual(len(stale), 1)

    def test_openstack_project_groups_skip_rows_without_identity(self):
        migrations = {"migrations": [
            {"target_platform": "openstack", "vm_name": "legacy"},
            {"target_platform": "openstack", "vm_name": "v",
             "openstack_project": "project-a"},
        ]}
        deployments = {"deployments": [
            {"target_platform": "openstack", "vm_name": "legacy"}]}
        self.assertEqual(
            reconcile.catalog_project_groups(migrations, deployments),
            [{"project-a"}])
        self.assertEqual(
            reconcile.catalog_project_groups(
                {"migrations": migrations["migrations"][:1]}, deployments), [])

    def test_openstack_reconcile_rejects_string_port_ids(self):
        entry = {"target_platform": "openstack", "openstack_project": "p",
                 "openstack_server_id": "s1", "openstack_port_ids": "abc"}
        connection = SimpleNamespace(
            get_server=lambda i: object(), get_volume=lambda i: object())
        with self.assertRaises(ValueError):
            reconcile.find_missing_resources(
                connection, {"migrations": []}, {"deployments": [entry]}, {"p"})

    def test_latest_migration_compares_instants(self):
        import lookup_migration_common as lookup
        rows = [
            {"vm_name": "v", "target_platform": "gcp", "status": "completed",
             "migrated_at": "2026-01-01T10:00:00+00:00", "id": "newest"},
            {"vm_name": "v", "target_platform": "gcp", "status": "completed",
             "migrated_at": "2026-01-01T11:00:00+05:00", "id": "older"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "c.json"
            path.write_text(json.dumps({"schema_version": 1, "migrations": rows}))
            self.assertEqual(lookup.latest_migration(path, "v", "gcp")["id"], "newest")

    def test_latest_migration_skips_unparseable_timestamps(self):
        import lookup_migration_common as lookup
        rows = [
            {"vm_name": "v", "target_platform": "gcp", "status": "completed",
             "migrated_at": "not-a-date", "id": "garbage"},
            {"vm_name": "w", "target_platform": "gcp", "status": "completed",
             "migrated_at": "2026-01-01T10:00:00", "id": "naive"},
            {"vm_name": "x", "target_platform": "gcp", "status": "completed",
             "migrated_at": "2026-01-01T10:00:00Z", "id": "good"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "c.json"
            path.write_text(json.dumps({"schema_version": 1, "migrations": rows}))
            self.assertIsNone(lookup.latest_migration(path, "v", "gcp"))
            self.assertIsNone(lookup.latest_migration(path, "w", "gcp"))
            self.assertEqual(lookup.latest_migration(path, "x", "gcp")["id"], "good")

    def test_aws_reconcile_scope_ignores_other_accounts_malformed_rows(self):
        migrations = {"migrations": [
            {"target_platform": "aws", "vm_name": "a", "aws_account_id": "111",
             "aws_region": "us-west-2"},
            {"target_platform": "aws", "vm_name": "b", "aws_account_id": "222"},
        ]}
        deployments = {"deployments": []}
        regions, accounts = reconcile_aws.catalog_scope(migrations, deployments, "111")
        self.assertEqual((regions, accounts), ({"us-west-2"}, {"111"}))
        with self.assertRaises(ValueError):
            reconcile_aws.catalog_scope(migrations, deployments, "222")
        with self.assertRaises(ValueError):
            reconcile_aws.catalog_scope(migrations, deployments)

    def test_aws_lookup_filters_region(self):
        lookup_ami = load_script("lookup-migration-ami.py", "lookup_ami")
        accept = lookup_ami.region_filter(SimpleNamespace(project=None, region="us-west-2"))
        self.assertTrue(accept({"aws_region": "us-west-2"}))
        self.assertFalse(accept({"aws_region": "us-east-1"}))

    def test_boot_disk_follows_controller_and_unit_not_instance_id(self):
        ovf = """<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
 xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
 xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1">
<References><File ovf:id="f1" ovf:href="a.vmdk"/><File ovf:id="f2" ovf:href="b.vmdk"/></References>
<DiskSection><Disk ovf:diskId="data" ovf:fileRef="f1" ovf:capacity="1" ovf:capacityAllocationUnits="byte * 2^30"/>
<Disk ovf:diskId="boot" ovf:fileRef="f2" ovf:capacity="1" ovf:capacityAllocationUnits="byte * 2^30"/></DiskSection>
<VirtualSystem ovf:id="vm"><VirtualHardwareSection>
<Item><rasd:Address>0</rasd:Address><rasd:InstanceID>3</rasd:InstanceID><rasd:ResourceType>6</rasd:ResourceType></Item>
<Item><rasd:AddressOnParent>1</rasd:AddressOnParent><rasd:HostResource>ovf:/disk/data</rasd:HostResource><rasd:InstanceID>9</rasd:InstanceID><rasd:Parent>3</rasd:Parent><rasd:ResourceType>17</rasd:ResourceType></Item>
<Item><rasd:AddressOnParent>0</rasd:AddressOnParent><rasd:HostResource>ovf:/disk/boot</rasd:HostResource><rasd:InstanceID>12</rasd:InstanceID><rasd:Parent>3</rasd:Parent><rasd:ResourceType>17</rasd:ResourceType></Item>
</VirtualHardwareSection></VirtualSystem></Envelope>"""
        import xml.etree.ElementTree as ET
        manifest = inspect_ova._inspect_descriptor(
            ET.fromstring(ovf), "ovf", "x.ovf", "x.ovf")
        self.assertEqual(manifest["disks"][0]["disk_id"], "boot")
        self.assertTrue(manifest["disks"][0]["boot_candidate"])
        # Descriptor lists "data" first; AWS would root-boot that one.
        self.assertEqual(manifest["disks"][0]["ovf_position"], 1)

    def test_gcp_absent_pattern_ignores_incidental_404(self):
        import re
        import yaml
        text = (ROOT / "playbooks/tasks/migration/gcp/initialize.yml").read_text()
        pattern = next(
            task["ansible.builtin.set_fact"]["gcp_absent_object_pattern"]
            for task in yaml.safe_load(text)
            if "gcp_absent_object_pattern" in task.get("ansible.builtin.set_fact", {}))
        self.assertTrue(re.search(pattern, "httperror 404: no such object"))
        self.assertTrue(re.search(pattern, "one or more urls matched no objects"))
        self.assertFalse(re.search(pattern, "permission denied, request id 4041-a404b"))

if __name__ == "__main__":
    unittest.main()
