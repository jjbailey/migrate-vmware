#!/usr/bin/env python3
# tests/test_list_image_catalog.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Exercise the image catalog report through its command-line interface."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "list-image-catalog.py"

class ListImageCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = Path(self.temp.name) / "catalog.json"
        common = {"vm_name": "vm-a", "status": "completed", "migrated_at": "2026-10-28T10:00:00Z"}
        self.entries = [
            {**common, "target_platform": "aws", "ami_id": "ami-one", "aws_project": "aws-key",
             "aws_account_id": "123456789012", "aws_region": "eu-west-1"},
            {**common, "target_platform": "openstack", "openstack_project": "os-key",
             "openstack_project_id": "os-id", "openstack_region": "RegionOne",
             "openstack_image_ids": ["boot-id", "data-id"], "openstack_image_names": ["boot", "data"]},
            {**common, "target_platform": "gcp", "gcp_project": "gcp-key", "gcp_import_location": "eu",
             "gcp_boot_image_name": "gcp-boot", "gcp_boot_image_selflink": "https://example/boot",
             "gcp_data_image_names": ["gcp-data"], "gcp_data_image_selflinks": ["https://example/data"]},
            {**common, "vm_name": "vm-b", "target_platform": "aws", "status": "retired", "ami_id": "ami-old"},
        ]
        self.write_catalog(self.entries)

    def write_catalog(self, entries):
        self.catalog.write_text(json.dumps({"schema_version": 1, "migrations": entries}))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--catalog", str(self.catalog), *args],
                              cwd=self.temp.name, capture_output=True, text=True, check=False)

    def selected(self, *args):
        result = self.run_cli("--json", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_default_report_includes_all_platform_images_without_changing_catalog(self):
        before = self.catalog.read_bytes()
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in ("Entries: 3 (completed)", "ami-one", "boot-id", "data-id", "gcp-boot", "gcp-data"):
            self.assertIn(text, result.stdout)
        self.assertLess(result.stdout.index("boot-id"), result.stdout.index("data-id"))
        self.assertNotIn("ami-old", result.stdout)
        self.assertEqual(self.catalog.read_bytes(), before)

    def test_filters_match_platform_specific_scope(self):
        for project, platform in (("aws-key", "aws"), ("123456789012", "aws"),
                                  ("os-key", "openstack"), ("os-id", "openstack"), ("gcp-key", "gcp")):
            with self.subTest(project=project):
                self.assertEqual([row["target_platform"] for row in self.selected("--project", project)], [platform])
        for region, platform in (("eu-west-1", "aws"), ("RegionOne", "openstack"), ("eu", "gcp")):
            with self.subTest(region=region):
                self.assertEqual([row["target_platform"] for row in self.selected("--region", region)], [platform])
        self.assertEqual(len(self.selected("--platform", "openstack", "--vm-name", "vm-a",
                                           "--project", "os-id", "--region", "RegionOne")), 1)
        self.assertEqual(self.selected("--provider", "aws", "--project", "os-key"), [])
        self.assertEqual(len(self.selected("--status", "all")), 4)
        self.assertEqual(len(self.selected("--status", "retired", "--vm-name", "vm-b")), 1)

    def test_sort_preserves_history_and_compares_timestamp_instants(self):
        base = self.entries[0]
        self.write_catalog([
            {**base, "clone_name": "older", "migrated_at": "2026-10-28T12:00:00+03:00"},
            {**base, "clone_name": "newer", "migrated_at": "2026-10-28T10:00:00Z"},
            {**base, "clone_name": "legacy", "migrated_at": "unknown"},
        ])
        self.assertEqual([row["clone_name"] for row in self.selected()], ["newer", "older", "legacy"])

    def test_empty_and_no_matches_are_successful(self):
        result = self.run_cli("--vm-name", "missing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No matching catalog entries.", result.stdout)
        self.write_catalog([])
        self.assertEqual(self.selected(), [])

    def test_legacy_rows_without_optional_fields(self):
        self.write_catalog([{"vm_name": "legacy", "target_platform": "openstack", "status": "completed",
                             "openstack_image_ids": ["legacy-image"]}])
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("legacy-image", result.stdout)
        self.assertEqual(self.selected("--region", "RegionOne"), [])

    def test_bad_catalogs_fail_cleanly(self):
        for payload in ("not json", "[]", '{"schema_version": 2, "migrations": []}',
                        '{"schema_version": 1, "migrations": [null]}',
                        '{"schema_version": 1, "migrations": [{"openstack_image_ids": "bad"}]}'):
            with self.subTest(payload=payload):
                self.catalog.write_text(payload)
                result = self.run_cli()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("list-image-catalog:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        self.catalog.unlink()
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no catalog", result.stderr)

    def test_help_describes_default_and_filters(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("migration-catalog.json", result.stdout)
        self.assertIn("--provider", result.stdout)

if __name__ == "__main__":
    unittest.main()