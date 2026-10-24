#!/usr/bin/env python3
# tests/test_gcp_catalog_tools.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""GCP catalog reconcile and lookup, against fakes and temporary catalogs.

Nothing here reaches Google: the reconcile tests hand find_missing_resources
a fake session, and main() runs with the credential loader and
AuthorizedSession patched out.
"""

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

def load_script(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

try:
    reconcile_gcp = load_script("reconcile-catalog-gcp.py", "reconcile_gcp")
except ImportError:  # google-auth is not installed
    reconcile_gcp = None
lookup_gcp = load_script("lookup-migration-gcp.py", "lookup_gcp")

API = "https://compute.googleapis.com/compute/v1/projects"

class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"status": "READY"}

class FakeSession:
    """Answers 404 for the URLs in missing, 200 for the rest, and counts calls."""

    def __init__(self, missing=(), errors=()):
        self.missing = set(missing)
        self.errors = set(errors)
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if url in self.errors:
            return FakeResponse(500)
        return FakeResponse(404 if url in self.missing else 200)

def image_url(project, name):
    return f"{API}/{project}/global/images/{name}"

def migration(vm, project="proj-a", status="completed", boot=None, data=()):
    boot = boot or f"{vm}-disk-0"
    return {
        "vm_name": vm, "target_platform": "gcp", "status": status,
        "gcp_project": project, "clone_name": f"{vm}-clone",
        "gcp_boot_image_name": boot, "gcp_data_image_names": list(data),
        "migrated_at": "2026-09-14T12:00:00Z",
    }

def deployment(vm, project="proj-a", status="RUNNING", zone="us-central1-a", disks=()):
    return {
        "vm_name": vm, "target_platform": "gcp", "status": status,
        "gcp_project": project, "gcp_zone": zone,
        "gcp_instance_name": f"{vm}-instance", "gcp_data_disk_names": list(disks),
        "launched_at": "2026-09-14T12:00:00Z",
    }

@unittest.skipIf(reconcile_gcp is None, "needs google-auth")
class GcpReconcileLogicTests(unittest.TestCase):

    def test_catalog_scope_collects_only_gcp_projects(self):
        migrations = {"migrations": [
            migration("a", project="proj-a"),
            {"target_platform": "aws", "gcp_project": "not-gcp"},
            dict(migration("b"), gcp_project="  "),
            dict(migration("c"), gcp_project=None),
        ]}
        deployments = {"deployments": [deployment("d", project="proj-b")]}
        self.assertEqual(reconcile_gcp.catalog_scope(migrations, deployments),
                         {"proj-a", "proj-b"})

    def test_validate_scope_rejects_a_project_the_catalog_lacks(self):
        migrations = {"migrations": [migration("a")]}
        deployments = {"deployments": []}
        reconcile_gcp.validate_scope(migrations, deployments, "proj-a")
        with self.assertRaisesRegex(ValueError, "no GCP entries for project proj-z"):
            reconcile_gcp.validate_scope(migrations, deployments, "proj-z")

    def test_missing_image_marks_only_that_migration_stale(self):
        gone = migration("gone", data=["gone-disk-1"])
        kept = migration("kept")
        session = FakeSession(missing={image_url("proj-a", "gone-disk-1")})
        stale, stale_deployments = reconcile_gcp.find_missing_resources(
            session, "proj-a", {"migrations": [gone, kept]}, {"deployments": []})
        self.assertEqual(stale, [(gone, ["gone-disk-1"])])
        self.assertEqual(stale_deployments, [])

    def test_out_of_scope_and_incomplete_rows_are_not_queried(self):
        rows = [
            migration("other-project", project="proj-b"),
            migration("failed", status="failed"),
            migration("retired", status="retired"),
            {"vm_name": "aws", "target_platform": "aws", "status": "completed",
             "gcp_project": "proj-a"},
        ]
        deployments = [deployment("retired", status="retired"),
                       deployment("elsewhere", project="proj-b")]
        session = FakeSession()
        stale, stale_deployments = reconcile_gcp.find_missing_resources(
            session, "proj-a", {"migrations": rows}, {"deployments": deployments})
        self.assertEqual((stale, stale_deployments), ([], []))
        self.assertEqual(session.calls, [])

    def test_shared_image_is_fetched_once(self):
        first = migration("a", boot="shared-disk-0")
        second = migration("b", boot="shared-disk-0")
        session = FakeSession(missing={image_url("proj-a", "shared-disk-0")})
        stale, _ = reconcile_gcp.find_missing_resources(
            session, "proj-a", {"migrations": [first, second]}, {"deployments": []})
        self.assertEqual([entry for entry, _ in stale], [first, second])
        self.assertEqual(session.calls, [image_url("proj-a", "shared-disk-0")])

    def test_server_error_is_not_treated_as_missing(self):
        session = FakeSession(errors={image_url("proj-a", "a-disk-0")})
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            reconcile_gcp.find_missing_resources(
                session, "proj-a", {"migrations": [migration("a")]}, {"deployments": []})

    def test_invalid_image_names_are_refused(self):
        entry = migration("a", data=[""])
        with self.assertRaisesRegex(ValueError, "invalid GCP image names"):
            reconcile_gcp.find_missing_resources(
                FakeSession(), "proj-a", {"migrations": [entry]}, {"deployments": []})

    def test_deployment_missing_instance_or_disk_is_stale(self):
        no_instance = deployment("vm-a")
        no_disk = deployment("vm-b", disks=["vm-b-data-1", "vm-b-data-2"])
        healthy = deployment("vm-c", disks=["vm-c-data-1"])
        zone = f"{API}/proj-a/zones/us-central1-a"
        session = FakeSession(missing={
            f"{zone}/instances/vm-a-instance",
            f"{zone}/disks/vm-b-data-2",
        })
        _, stale = reconcile_gcp.find_missing_resources(
            session, "proj-a", {"migrations": []},
            {"deployments": [no_instance, no_disk, healthy]})
        self.assertEqual(stale, [(no_instance, True, []),
                                 (no_disk, False, ["vm-b-data-2"])])

    def test_deployment_without_zone_or_disk_list_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no gcp_instance_name or gcp_zone"):
            reconcile_gcp.find_missing_resources(
                FakeSession(), "proj-a", {"migrations": []},
                {"deployments": [dict(deployment("a"), gcp_zone="")]})
        with self.assertRaisesRegex(ValueError, "invalid gcp_data_disk_names"):
            reconcile_gcp.find_missing_resources(
                FakeSession(), "proj-a", {"migrations": []},
                {"deployments": [dict(deployment("a"), gcp_data_disk_names="disk")]})

    def test_candidate_count_matches_the_rows_find_missing_resources_checks(self):
        migrations = {"migrations": [
            migration("a"), migration("b", status="failed"),
            migration("c", project="proj-b")]}
        deployments = {"deployments": [
            deployment("a"), deployment("b", status="retired"),
            deployment("c", project="proj-b")]}
        self.assertEqual(
            reconcile_gcp.candidate_count(migrations, deployments, "proj-a"), 2)

@unittest.skipIf(reconcile_gcp is None, "needs google-auth")
class GcpReconcileMainTests(unittest.TestCase):
    """main() end to end on temporary catalogs, with Google patched out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.migrations = self.dir / "migration-catalog.json"
        self.deployments = self.dir / "deployment-catalog.json"
        self.credentials = self.dir / "key.json"
        self.credentials.write_text("{}")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, migrations, deployments):
        self.migrations.write_text(json.dumps(
            {"schema_version": 1, "migrations": migrations}))
        self.deployments.write_text(json.dumps(
            {"schema_version": 1, "deployments": deployments}))

    def run_main(self, *extra, session=None, credential_project="proj-a"):
        argv = ["reconcile-catalog-gcp.py",
                "--migration-catalog", str(self.migrations),
                "--deployment-catalog", str(self.deployments),
                "--credentials-file", str(self.credentials), *extra]
        credentials = SimpleNamespace(project_id=credential_project)
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(reconcile_gcp.google.auth, "load_credentials_from_file",
                                  return_value=(credentials, None)), \
                mock.patch.object(reconcile_gcp, "AuthorizedSession",
                                  return_value=session or FakeSession()), \
                contextlib.redirect_stdout(output):
            status = reconcile_gcp.main()
        return status, output.getvalue()

    def names(self, path, list_key):
        return sorted(row["vm_name"] for row in json.loads(path.read_text())[list_key])

    def test_removes_stale_and_retired_rows_only(self):
        self.write(
            [migration("gone"), migration("kept"), migration("old", status="retired"),
             migration("other", project="proj-b")],
            [deployment("gone"), deployment("kept"), deployment("kept-2"),
             deployment("old", status="retired")])
        zone = f"{API}/proj-a/zones/us-central1-a"
        session = FakeSession(missing={image_url("proj-a", "gone-disk-0"),
                                       f"{zone}/instances/gone-instance"})
        status, output = self.run_main("--project-id", "proj-a", session=session)
        self.assertEqual(status, 0)
        self.assertIn("removed 2 migration and 2 deployment entries", output)
        self.assertEqual(self.names(self.migrations, "migrations"), ["kept", "other"])
        self.assertEqual(self.names(self.deployments, "deployments"), ["kept", "kept-2"])

    def test_dry_run_writes_nothing(self):
        self.write([migration("gone"), migration("kept")], [])
        before = self.migrations.read_text()
        session = FakeSession(missing={image_url("proj-a", "gone-disk-0")})
        status, output = self.run_main("--dry-run", session=session)
        self.assertEqual(status, 0)
        self.assertIn("1 entries would be removed", output)
        self.assertEqual(self.migrations.read_text(), before)

    def test_refuses_to_remove_most_rows_without_force(self):
        self.write([migration("a"), migration("b"), migration("c")], [])
        missing = {image_url("proj-a", f"{vm}-disk-0") for vm in ("a", "b")}
        before = self.migrations.read_text()
        with self.assertRaisesRegex(ValueError, "refusing to remove 2 of 3"):
            self.run_main(session=FakeSession(missing=missing))
        self.assertEqual(self.migrations.read_text(), before)
        status, _ = self.run_main("--force", session=FakeSession(missing=missing))
        self.assertEqual(status, 0)
        self.assertEqual(self.names(self.migrations, "migrations"), ["c"])

    def test_refuses_credentials_for_another_project(self):
        self.write([migration("a")], [])
        with self.assertRaisesRegex(ValueError, "belong to project proj-b"):
            self.run_main(credential_project="proj-b")

    def test_needs_project_id_when_catalog_spans_projects(self):
        self.write([migration("a"), migration("b", project="proj-b")], [])
        with self.assertRaisesRegex(ValueError, "multiple GCP projects"):
            self.run_main()

class GcpLookupTests(unittest.TestCase):

    def test_project_filter(self):
        self.assertIsNone(lookup_gcp.project_filter(SimpleNamespace(project=None)))
        accept = lookup_gcp.project_filter(SimpleNamespace(project=" proj-a "))
        self.assertTrue(accept({"gcp_project": "proj-a"}))
        self.assertFalse(accept({"gcp_project": "proj-b"}))
        self.assertFalse(accept({}))

    def test_projection_defaults_data_images_and_names_the_run(self):
        entry = dict(migration("a"), gcp_boot_image_selflink="https://example/a")
        del entry["gcp_data_image_names"]
        projected = lookup_gcp.project(entry)
        self.assertEqual(projected["gcp_data_image_names"], [])
        self.assertEqual(projected["gcp_data_image_selflinks"], [])
        self.assertEqual(projected["migration_id"], "a-clone")
        with self.assertRaises(KeyError):
            lookup_gcp.project({"vm_name": "a"})

    def lookup(self, rows, *extra):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "migration-catalog.json"
            path.write_text(json.dumps({"schema_version": 1, "migrations": rows}))
            return subprocess.run(
                [sys.executable, str(ROOT / "bin" / "lookup-migration-gcp.py"),
                 "--catalog", str(path), "--vm-name", "vm-a", *extra],
                capture_output=True, text=True, check=False)

    def row(self, project, migrated_at, image, **overrides):
        return dict(migration("vm-a", project=project, boot=image),
                    gcp_boot_image_selflink=f"https://example/{image}",
                    migrated_at=migrated_at, **overrides)

    def test_cli_returns_the_newest_completed_row_for_the_project(self):
        rows = [
            self.row("proj-a", "2026-09-01T00:00:00Z", "old-disk-0"),
            self.row("proj-a", "2026-09-02T00:00:00Z", "new-disk-0"),
            self.row("proj-a", "2026-09-03T00:00:00Z", "failed-disk-0", status="failed"),
            self.row("proj-b", "2026-09-04T00:00:00Z", "other-disk-0"),
        ]
        found = self.lookup(rows, "--project", "proj-a")
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(json.loads(found.stdout)["gcp_boot_image_name"], "new-disk-0")
        unfiltered = self.lookup(rows)
        self.assertEqual(json.loads(unfiltered.stdout)["gcp_boot_image_name"], "other-disk-0")

    def test_cli_fails_cleanly_when_nothing_matches(self):
        found = self.lookup([self.row("proj-a", "2026-09-01T00:00:00Z", "a-disk-0")],
                            "--project", "proj-z")
        self.assertNotEqual(found.returncode, 0)
        self.assertIn("lookup-migration-gcp: no usable", found.stderr)
        self.assertNotIn("Traceback", found.stderr)

if __name__ == "__main__":
    unittest.main()
