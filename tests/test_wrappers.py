#!/usr/bin/env python3
# tests/test_wrappers.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""How the bin/ wrappers turn environment variables into ansible -e options.

Each wrapper runs for real, down through bin/run-playbook.sh and
bin/ansible-run.sh, but a stub ansible-playbook first on PATH records its
argv instead of running anything.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"

STUB = """#!/bin/sh
python3 -c 'import json, sys; json.dump(sys.argv[1:], open(sys.argv[1], "w"))' \
    "$WRAPPER_TEST_ARGV" "$@"
"""

class WrapperTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        stub_bin = self.tmp / "stub-bin"
        stub_bin.mkdir()
        stub = stub_bin / "ansible-playbook"
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.argv_file = self.tmp / "argv.json"
        # An empty HOME has no ~/.ssh/dib-vault-pass, so ansible-run.sh
        # appends --ask-vault-pass, which the stub ignores.
        home = self.tmp / "home"
        home.mkdir()
        self.base_env = {
            "PATH": f"{stub_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(home),
            "WRAPPER_TEST_ARGV": str(self.argv_file),
        }

    def run_wrapper(self, script, env=None, args=(), expect_success=True):
        result = subprocess.run(
            [str(BIN / script), *args],
            env={**self.base_env, **(env or {})},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            check=False, timeout=60,
        )
        if expect_success:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def recorded_argv(self):
        argv = json.loads(self.argv_file.read_text())
        # argv[0] is the stub's own output path.
        return argv[1:]

    def extra_vars(self):
        """Merge every -e in order, as ansible does (later ones win)."""
        argv = self.recorded_argv()
        merged = {}
        for index, arg in enumerate(argv):
            if arg != "-e":
                continue
            value = argv[index + 1]
            # Every value must be a JSON object, never key=value, which
            # ansible would split on whitespace.
            self.assertTrue(value.startswith("{"), value)
            merged.update(json.loads(value))
        return merged

class MigrateWrapperTests(WrapperTestCase):
    SCRIPTS = {
        "migrate-aws-vm.sh": "playbooks/migrate-aws-vm.yml",
        "migrate-gcp-vm.sh": "playbooks/migrate-gcp-vm.yml",
        "migrate-openstack-vm.sh": "playbooks/migrate-openstack-vm.yml",
        "migrate-openstack-vm-vddk.sh": "playbooks/migrate-openstack-vm-vddk.yml",
    }

    def test_values_with_spaces_arrive_whole(self):
        for script, playbook in self.SCRIPTS.items():
            with self.subTest(script=script):
                self.run_wrapper(script, {
                    "VM_NAME": "Web Server 01",
                    "VCENTER_DATACENTER": "DC One",
                    "OS_PROJECT_NAME": "project a",
                    "GCP_TARGET_PROJECT": "project a",
                    "AWS_TARGET_PROJECT": "account a",
                })
                argv = self.recorded_argv()
                self.assertEqual(argv[0], playbook)
                extra = self.extra_vars()
                self.assertEqual(extra["vm_name"], "Web Server 01")
                self.assertEqual(extra["datacenter"], "DC One")
                self.assertEqual(extra["quiesce_snapshot"], "true")
                self.assertNotIn("vsphere_target_vcenter", extra)
                self.assertNotIn("validate_certs", extra)

    def test_artifact_root_defaults_to_var_tmp(self):
        for script in self.SCRIPTS:
            with self.subTest(script=script):
                self.run_wrapper(script, {"VM_NAME": "vm"})
                self.assertEqual(
                    self.extra_vars()["artifact_root"], "/var/tmp/migrate-vmware")
                self.run_wrapper(script, {
                    "VM_NAME": "vm", "ARTIFACT_ROOT": "/srv/my artifacts"})
                self.assertEqual(
                    self.extra_vars()["artifact_root"], "/srv/my artifacts")

    def test_vcenter_wins_over_datacenter(self):
        self.run_wrapper("migrate-aws-vm.sh", {
            "VM_NAME": "vm", "VSPHERE_TARGET_VCENTER": "vc one",
            "VCENTER_DATACENTER": "dc"})
        extra = self.extra_vars()
        self.assertEqual(extra["vsphere_target_vcenter"], "vc one")
        self.assertNotIn("datacenter", extra)

    def test_vddk_options(self):
        self.run_wrapper("migrate-openstack-vm-vddk.sh", {
            "VM_NAME": "vm", "BACKEND": "curl", "COMPRESS": "true",
            "VCENTER_CA_BUNDLE": "/etc/ssl/my ca.pem"})
        extra = self.extra_vars()
        self.assertEqual(extra["openstack_vddk_backend"], "curl")
        self.assertEqual(extra["openstack_convert_compress"], "true")
        self.assertEqual(extra["openstack_vddk_ca_bundle"], "/etc/ssl/my ca.pem")
        self.assertNotIn("openstack_vddk_require_cbt", extra)
        self.assertNotIn("discard_run_id", extra)

    def test_extra_args_follow_the_wrapper_vars(self):
        self.run_wrapper(
            "migrate-aws-vm.sh", {"VM_NAME": "vm"},
            args=["-e", '{"vm_name": "override"}'])
        argv = self.recorded_argv()
        self.assertEqual(self.extra_vars()["vm_name"], "override")
        self.assertIn("-v", argv)

    def test_bad_bool_is_rejected(self):
        result = self.run_wrapper(
            "migrate-aws-vm.sh", {"VALIDATE_CERTS": "yes"},
            expect_success=False)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.argv_file.exists())

class LaunchWrapperTests(WrapperTestCase):
    def test_openstack_user_data_and_spaced_json(self):
        user_data = "#cloud-config\npackages:\n  - vim\nruncmd:\n  - echo 'a b'\n"
        self.run_wrapper("launch-openstack-instance.sh", {
            "VM_NAME": "Web Server 01",
            "OS_USER_DATA": user_data,
            "OS_PRESERVE_MAC": "true",
            "OS_MAC_ADDRESSES_JSON": '[ "aa:bb:cc:11:22:33", "aa:bb:cc:11:22:34" ]',
            "OS_FIXED_IPS_JSON": '[ "10.0.0.5", "10.0.0.6" ]',
            "OS_SECURITY_GROUPS_JSON": '[ "default", "web servers" ]',
            "OS_PROJECT_NAME": "project a",
        })
        extra = self.extra_vars()
        self.assertEqual(extra["vm_name"], "Web Server 01")
        self.assertEqual(extra["openstack_user_data"], user_data)
        self.assertEqual(extra["openstack_mac_addresses"],
                         ["aa:bb:cc:11:22:33", "aa:bb:cc:11:22:34"])
        self.assertEqual(extra["openstack_fixed_ips"], ["10.0.0.5", "10.0.0.6"])
        self.assertEqual(extra["openstack_security_groups"], ["default", "web servers"])
        self.assertEqual(extra["openstack_target_project"], "project a")
        self.assertEqual(extra["openstack_preserve_mac"], "true")
        self.assertEqual(extra["openstack_key_name"], "cloud")
        self.assertNotIn("openstack_security_group_ids", extra)
        self.assertNotIn("openstack_nics", extra)
        self.assertNotIn("openstack_user_data_file", extra)

    def test_openstack_security_group_ids_clear_names(self):
        self.run_wrapper("launch-openstack-instance.sh", {
            "VM_NAME": "vm",
            "OS_SECURITY_GROUP_IDS_JSON": '[ "1111", "2222" ]'})
        extra = self.extra_vars()
        self.assertEqual(extra["openstack_security_groups"], [])
        self.assertEqual(extra["openstack_security_group_ids"], ["1111", "2222"])

    def test_openstack_nics_clear_network(self):
        nics = '[ {"net-id": "n1", "v4-fixed-ip": "10.0.0.5"} ]'
        self.run_wrapper("launch-openstack-instance.sh", {
            "VM_NAME": "vm", "OS_NETWORK": "Ext-Net", "OS_NICS_JSON": nics})
        extra = self.extra_vars()
        self.assertEqual(extra["openstack_network"], "")
        self.assertEqual(extra["openstack_nics"],
                         [{"net-id": "n1", "v4-fixed-ip": "10.0.0.5"}])

    def test_openstack_rejects_non_array_json(self):
        result = self.run_wrapper(
            "launch-openstack-instance.sh",
            {"VM_NAME": "vm", "OS_FIXED_IPS_JSON": '"10.0.0.5"'},
            expect_success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.argv_file.exists())

    def test_aws(self):
        self.run_wrapper("launch-aws-instance.sh", {
            "VM_NAME": "Web Server 01",
            "AWS_SECURITY_GROUPS_JSON": '[ "sg-1", "sg-2" ]',
            "AWS_SUBNET_ID": "subnet-1"})
        extra = self.extra_vars()
        self.assertEqual(extra["vm_name"], "Web Server 01")
        self.assertEqual(extra["security_groups"], ["sg-1", "sg-2"])
        self.assertEqual(extra["vpc_subnet_id"], "subnet-1")
        self.assertEqual(extra["key_name"], "cloud")
        self.assertNotIn("instance_type", extra)

    def test_gcp(self):
        key = self.tmp / "my key.pub"
        key.write_text("ssh-ed25519 AAAA test\n")
        self.run_wrapper("launch-gcp-instance.sh", {
            "VM_NAME": "Web Server 01",
            "GCP_TAGS_JSON": '[ "web", "ssh" ]',
            "GCP_NO_ADDRESS": "true",
            "GCP_SSH_KEY_FILE": str(key)})
        extra = self.extra_vars()
        self.assertEqual(extra["vm_name"], "Web Server 01")
        self.assertEqual(extra["gcp_tags"], ["web", "ssh"])
        self.assertEqual(extra["gcp_no_address"], "true")
        self.assertEqual(extra["gcp_network"], "default")
        self.assertEqual(extra["gcp_ssh_public_key_file"], str(key))
        self.assertEqual(extra["gcp_ssh_user"], "cloudinit")

    def test_gcp_defaults_leave_tags_out(self):
        self.run_wrapper("launch-gcp-instance.sh", {"VM_NAME": "vm"})
        extra = self.extra_vars()
        self.assertNotIn("gcp_tags", extra)
        self.assertNotIn("gcp_no_address", extra)
        self.assertNotIn("gcp_ssh_user", extra)

class StageV2vTests(WrapperTestCase):
    def test_values_with_spaces(self):
        self.run_wrapper("stage-v2v.sh", args=[
            "-c", "vc one", "-H", "vm1", "-u", "admin user",
            "-k", "/keys/my key"])
        argv = self.recorded_argv()
        self.assertEqual(argv[0], "playbooks/stage-v2v.yml")
        extra = self.extra_vars()
        self.assertEqual(extra, {
            "vsphere_target_vcenter": "vc one",
            "ssh_username": "admin user",
            "ssh_private_key_file": "/keys/my key",
        })

class DefaultsTests(unittest.TestCase):
    def test_no_wrapper_defaults_under_work(self):
        for path in sorted(BIN.glob("*.sh")):
            with self.subTest(script=path.name):
                self.assertNotIn("/work/migrate-vmware", path.read_text())

if __name__ == "__main__":
    unittest.main()
