#!/usr/bin/env python3
# tests/test_append_catalogs.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""The append scripts' main(): locked, atomic, sorted, idempotent writes.

Each test drives main() the way the playbooks do, with --catalog and an
--entry file, against a catalog in a temporary directory.
"""

import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import catalog_common  # noqa: E402

def load_script(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

migration = load_script("append-migration-catalog.py", "append_migration_main")
deployment = load_script("append-deployment-catalog.py", "append_deployment_main")

def aws_migration(vm, task, migrated_at="2026-09-14T12:00:00Z", account="111", **extra):
    return {
        "vm_name": vm, "source_vcenter": "vc.example", "source_vcenter_key": "vc",
        "source_datacenter": "dc", "clone_name": f"{vm}-clone",
        "snapshot_name": f"{vm}-snap", "target_platform": "aws",
        "artifact_type": "ova",
        "artifact_manifest": {"manifest_version": 1, "disk_count": 1,
                              "disks": [{"index": 0, "boot_candidate": True}]},
        "status": "completed", "migrated_at": migrated_at,
        "s3_bucket": "bucket", "s3_key": f"{vm}.ova", "aws_region": "us-west-2",
        "ami_id": f"ami-{task}", "ami_name": vm, "import_task_id": task,
        "aws_account_id": account, **extra,
    }

def openstack_deployment(vm, server, launched_at="2026-09-14T12:00:00Z",
                         project="project-a", region="region-a", **extra):
    return {
        "vm_name": vm, "target_platform": "openstack", "status": "ACTIVE",
        "launched_at": launched_at, "openstack_server_id": server,
        "openstack_image_ids": ["image-boot"], "openstack_project": project,
        "openstack_region": region, "openstack_flavor": "small",
        "openstack_network": "net-a", "openstack_data_volume_ids": [], **extra,
    }

def gcp_deployment(vm, instance, zone="us-central1-a", **extra):
    return {
        "vm_name": vm, "target_platform": "gcp", "status": "RUNNING",
        "launched_at": "2026-09-14T12:00:00Z", "gcp_instance_name": instance,
        "gcp_project": "proj-a", "gcp_zone": zone, "gcp_machine_type": "e2-small",
        "gcp_boot_image_name": f"{vm}-disk-0", "gcp_data_disk_names": [], **extra,
    }

class AppendMainTestCase(unittest.TestCase):
    module = None
    list_key = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # A directory that does not exist yet: main() must create it.
        self.catalog = self.dir / "catalogs" / "catalog.json"

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, entry):
        entry_path = self.dir / "entry.json"
        entry_path.write_text(json.dumps(entry))
        argv = ["append", "--catalog", str(self.catalog), "--entry", str(entry_path)]
        with mock.patch.object(sys, "argv", argv):
            self.module.main()

    def rows(self):
        return json.loads(self.catalog.read_text())[self.list_key]

class AppendMigrationMainTests(AppendMainTestCase):
    module = migration
    list_key = "migrations"

    def test_first_append_creates_the_catalog(self):
        self.append(aws_migration("vm-a", "import-ami-1"))
        catalog = json.loads(self.catalog.read_text())
        self.assertEqual(catalog["schema_version"], 1)
        self.assertEqual([row["import_task_id"] for row in catalog["migrations"]],
                         ["import-ami-1"])
        self.assertIsNotNone(catalog_common.parse_timestamp(catalog["generated_at"]))
        # Only the catalog and its sidecar lock remain: no temporary file.
        self.assertEqual(sorted(path.name for path in self.catalog.parent.iterdir()),
                         ["catalog.json", "catalog.json.lock"])

    def test_rows_are_sorted_by_vm_then_time(self):
        self.append(aws_migration("vm-b", "import-ami-1", "2026-09-14T12:00:00Z"))
        # Later wall-clock string, earlier instant: sorting must parse it.
        self.append(aws_migration("vm-a", "import-ami-2", "2026-09-14T13:00:00+02:00"))
        self.append(aws_migration("vm-a", "import-ami-3", "2026-09-14T12:00:00Z"))
        self.assertEqual([row["import_task_id"] for row in self.rows()],
                         ["import-ami-2", "import-ami-3", "import-ami-1"])

    def test_rerun_replaces_the_same_import_and_keeps_history(self):
        self.append(aws_migration("vm-a", "import-ami-1"))
        self.append(aws_migration("vm-a", "import-ami-2", "2026-09-15T12:00:00Z"))
        self.append(aws_migration("vm-a", "import-ami-1", ami_name="retried"))
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        replaced = next(row for row in rows if row["import_task_id"] == "import-ami-1")
        self.assertEqual(replaced["ami_name"], "retried")

    def test_same_task_id_in_another_account_is_a_different_migration(self):
        self.append(aws_migration("vm-a", "import-ami-1", account="111"))
        self.append(aws_migration("vm-a", "import-ami-1", account="222"))
        self.assertEqual(sorted(row["aws_account_id"] for row in self.rows()),
                         ["111", "222"])

    def test_invalid_entry_leaves_the_catalog_untouched(self):
        self.append(aws_migration("vm-a", "import-ami-1"))
        before = self.catalog.read_text()
        bad = aws_migration("vm-b", "import-ami-2")
        del bad["ami_id"]
        with self.assertRaisesRegex(ValueError, "ami_id"):
            self.append(bad)
        self.assertEqual(self.catalog.read_text(), before)

    def test_existing_file_mode_is_kept(self):
        self.append(aws_migration("vm-a", "import-ami-1"))
        os.chmod(self.catalog, 0o600)
        self.append(aws_migration("vm-b", "import-ami-2"))
        self.assertEqual(stat.S_IMODE(self.catalog.stat().st_mode), 0o600)

    def test_append_waits_for_the_catalog_lock(self):
        self.append(aws_migration("vm-a", "import-ami-1"))
        errors = []

        def writer():
            try:
                self.append(aws_migration("vm-b", "import-ami-2"))
            except Exception as error:  # surfaced by the assertion below
                errors.append(error)

        with catalog_common.exclusive_lock(self.catalog):
            thread = threading.Thread(target=writer)
            thread.start()
            thread.join(timeout=0.5)
            # flock is per open file, so the writer blocks on our lock.
            self.assertTrue(thread.is_alive(), "append did not wait for the lock")
            self.assertEqual(len(self.rows()), 1)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.rows()), 2)

class AppendDeploymentMainTests(AppendMainTestCase):
    module = deployment
    list_key = "deployments"

    def test_rerun_replaces_the_same_server_and_keeps_history(self):
        self.append(openstack_deployment("vm-a", "server-1"))
        self.append(openstack_deployment("vm-a", "server-2", "2026-09-15T12:00:00Z"))
        self.append(openstack_deployment("vm-a", "server-1", status="SHUTOFF"))
        rows = self.rows()
        self.assertEqual([row["openstack_server_id"] for row in rows],
                         ["server-1", "server-2"])
        self.assertEqual(rows[0]["status"], "SHUTOFF")

    def test_same_server_id_in_another_project_or_region_is_kept(self):
        self.append(openstack_deployment("vm-a", "server-1"))
        self.append(openstack_deployment("vm-a", "server-1", project="project-b"))
        self.append(openstack_deployment("vm-a", "server-1", region="region-b"))
        self.assertEqual(len(self.rows()), 3)

    def test_gcp_identity_is_instance_project_and_zone(self):
        self.append(gcp_deployment("vm-a", "instance-1"))
        self.append(gcp_deployment("vm-a", "instance-1", zone="us-east1-b"))
        self.append(gcp_deployment("vm-a", "instance-1", status="TERMINATED"))
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {(row["gcp_zone"], row["status"]) for row in rows},
            {("us-central1-a", "TERMINATED"), ("us-east1-b", "RUNNING")})

    def test_rows_are_sorted_by_vm_then_launch_time(self):
        self.append(openstack_deployment("vm-b", "server-1"))
        self.append(openstack_deployment("vm-a", "server-2", "2026-09-15T12:00:00Z"))
        self.append(openstack_deployment("vm-a", "server-3", "2026-09-14T12:00:00Z"))
        self.assertEqual([row["openstack_server_id"] for row in self.rows()],
                         ["server-3", "server-2", "server-1"])

    def test_invalid_entry_leaves_the_catalog_untouched(self):
        self.append(openstack_deployment("vm-a", "server-1"))
        before = self.catalog.read_text()
        with self.assertRaisesRegex(ValueError, "openstack_image_ids"):
            self.append(openstack_deployment("vm-b", "server-2", openstack_image_ids=[]))
        self.assertEqual(self.catalog.read_text(), before)

if __name__ == "__main__":
    unittest.main()
