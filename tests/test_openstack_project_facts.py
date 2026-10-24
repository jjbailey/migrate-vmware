#!/usr/bin/env python3
# tests/test_openstack_project_facts.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Tests for bin/openstack-project-facts.py, using fake connection objects."""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import openstack

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "openstack-project-facts.py"
spec = importlib.util.spec_from_file_location("openstack_project_facts", SCRIPT)
facts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(facts)

PROJECT = "p-mine"

def policy(object_id, target, owner="p-admin"):
    return SimpleNamespace(object_id=object_id, target_project_id=target,
                           project_id=owner)

def network(network_id, owner="p-admin", shared=False, external=False):
    return SimpleNamespace(id=network_id, name=network_id, project_id=owner,
                           is_shared=shared, is_router_external=external,
                           status="ACTIVE")

def group(group_id, owner="p-other", shared=False):
    return SimpleNamespace(id=group_id, name=group_id, project_id=owner,
                           is_shared=shared, security_group_rules=[],
                           description="")

class FakeNetwork:
    """conn.network with RBAC policies keyed by (object_type, action).

    A policy list of None makes the listing fail, as Neutron does for a user
    not allowed to list policies.
    """

    def __init__(self, policies=None, networks=(), groups=()):
        self.policies = policies or {}
        self._networks = list(networks)
        self._groups = list(groups)

    def rbac_policies(self, object_type, action):
        found = self.policies.get((object_type, action), [])
        if found is None:
            raise openstack.exceptions.ForbiddenException("not allowed")
        yield from found

    def networks(self):
        return iter(self._networks)

    def subnets(self):
        return iter([SimpleNamespace(network_id="n-own", cidr="10.0.0.0/24")])

    def security_groups(self):
        return iter(self._groups)

def connection(**kwargs):
    return SimpleNamespace(network=FakeNetwork(**kwargs))

class QuotaRowTests(unittest.TestCase):
    def row(self, used, limit, unit="", warn=80):
        return facts.quota_row("compute", "cores", used, limit, unit, warn)

    def test_unlimited(self):
        for limit in (None, -1):
            with self.subTest(limit=limit):
                row = self.row(5, limit)
                self.assertEqual(row["used_total"], "5/unlimited")
                self.assertIsNone(row["limit"])
                self.assertEqual((row["free"], row["percent"], row["status"]),
                                 ("", "", ""))

    def test_zero_limit(self):
        row = self.row(1, 0)
        self.assertEqual(row["used_total"], "1/0")
        self.assertEqual((row["free"], row["percent"], row["status"]),
                         (0, "", "none allowed"))

    def test_full_and_over(self):
        for used in (10, 12):
            with self.subTest(used=used):
                row = self.row(used, 10)
                self.assertEqual(row["status"], "FULL")
                self.assertEqual(row["free"], 0)

    def test_low_threshold(self):
        self.assertEqual(self.row(8, 10)["status"], "LOW")
        self.assertEqual(self.row(7, 10)["status"], "")
        self.assertEqual(self.row(5, 10, warn=50)["status"], "LOW")

    def test_missing_usage_and_unit(self):
        row = self.row(None, 4, unit="GB")
        self.assertEqual(row["used"], 0)
        self.assertEqual(row["used_total"], "0/4")
        self.assertEqual(row["percent"], "0%")
        self.assertEqual(row["resource"], "cores (GB)")

class SecurityGroupTests(unittest.TestCase):
    GROUPS = [group("sg-own", owner=PROJECT), group("sg-rbac"),
              group("sg-flagged", shared=True), group("sg-other")]

    def names(self, conn):
        return {g["id"]: g["access"]
                for g in facts.gather_security_groups(conn, PROJECT)}

    def test_rbac_policies_listed(self):
        conn = connection(groups=self.GROUPS, policies={
            ("security_group", "access_as_shared"): [
                policy("sg-rbac", PROJECT), policy("sg-other", "p-else")]})
        self.assertEqual(self.names(conn), {
            "sg-own": "owned", "sg-rbac": "rbac-shared",
            "sg-flagged": "rbac-shared"})

    def test_is_shared_fallback_when_rbac_listing_fails(self):
        conn = connection(groups=self.GROUPS, policies={
            ("security_group", "access_as_shared"): None})
        self.assertEqual(self.names(conn), {
            "sg-own": "owned", "sg-flagged": "rbac-shared"})

class NetworkTests(unittest.TestCase):
    def access(self, conn):
        return {n["id"]: n["access"]
                for n in facts.gather_networks(conn, PROJECT)}

    def test_owned_and_shared(self):
        conn = connection(networks=[
            network("n-own", owner=PROJECT), network("n-shared", shared=True),
            network("n-rbac"), network("n-private")], policies={
            ("network", "access_as_shared"): [policy("n-rbac", PROJECT)]})
        self.assertEqual(self.access(conn), {
            "n-own": "owned", "n-shared": "shared", "n-rbac": "shared"})

    def test_admin_sees_only_external_networks_open_to_the_project(self):
        conn = connection(networks=[
            network("ext-all", external=True),
            network("ext-mine", external=True),
            network("ext-theirs", external=True),
            network("ext-none", external=True)], policies={
            ("network", "access_as_external"): [
                policy("ext-all", "*"), policy("ext-mine", PROJECT),
                policy("ext-theirs", "p-else")]})
        self.assertEqual(self.access(conn), {
            "ext-all": "external", "ext-mine": "external"})

    def test_non_admin_keeps_every_listed_external_network(self):
        networks = [network("ext-a", external=True),
                    network("ext-b", external=True)]
        # Listing refused outright.
        conn = connection(networks=networks, policies={
            ("network", "access_as_shared"): None,
            ("network", "access_as_external"): None})
        self.assertEqual(self.access(conn), {
            "ext-a": "external", "ext-b": "external"})
        # Listing allowed, but Neutron only shows the user's own policies.
        conn = connection(networks=networks, policies={
            ("network", "access_as_external"): [
                policy("own-ext", "p-else", owner=PROJECT)]})
        self.assertEqual(self.access(conn), {
            "ext-a": "external", "ext-b": "external"})

    def test_external_flag_kept_on_shared_network(self):
        conn = connection(networks=[
            network("ext-shared", shared=True, external=True)], policies={
            ("network", "access_as_external"): [policy("x", "*")]})
        rows = facts.gather_networks(conn, PROJECT)
        self.assertEqual([(r["access"], r["external"]) for r in rows],
                         [("shared", True)])

class ConnectTests(unittest.TestCase):
    def args(self, **overrides):
        values = {"cloud": None, "project": None, "region_name": None,
                  "vault": Path("vaults/openstack.yml")}
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_vault_path_ignores_environment_and_clouds_yaml(self):
        auth = {"auth_url": "https://keystone.example/v3", "username": "u",
                "password": "p", "project_id": 1234, "project_name": ""}
        with mock.patch.object(facts, "select_vault_auth",
                               return_value=("key", auth)), \
                mock.patch.object(facts.openstack, "connect") as connect:
            facts.connect(self.args(project="key", region_name="R1"))
        connect.assert_called_once_with(
            load_envvars=False, load_yaml_config=False,
            auth_url="https://keystone.example/v3", username="u",
            password="p", project_id="1234", region_name="R1")

    def test_cloud_warns_about_ignored_project(self):
        stderr = io.StringIO()
        with mock.patch.object(facts.openstack, "connect") as connect, \
                contextlib.redirect_stderr(stderr):
            facts.connect(self.args(cloud="mycloud", project="proj"))
        connect.assert_called_once_with(cloud="mycloud", region_name=None)
        self.assertIn("ignoring PROJECT 'proj'", stderr.getvalue())
        stderr = io.StringIO()
        with mock.patch.object(facts.openstack, "connect"), \
                contextlib.redirect_stderr(stderr):
            facts.connect(self.args(cloud="mycloud"))
        self.assertEqual(stderr.getvalue(), "")

    def test_several_projects_message_names_the_positional_argument(self):
        data = {"openstack_projects": {"a": {}, "b": {}}}
        with mock.patch.object(facts, "read_vault", return_value=data):
            with self.assertRaises(SystemExit) as raised:
                facts.select_vault_auth(Path("v.yml"), None)
        self.assertIn("PROJECT argument", str(raised.exception))
        self.assertNotIn("--project", str(raised.exception))

class CommandLineTests(unittest.TestCase):
    def test_unknown_cloud_is_one_line(self):
        with tempfile.TemporaryDirectory() as home:
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("OS_")}
            env["HOME"] = home
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--cloud", "no-such-cloud"],
                cwd=home, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, check=False, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(result.stderr.startswith("openstack-project-facts: "),
                        result.stderr)

if __name__ == "__main__":
    unittest.main()
