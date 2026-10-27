#!/usr/bin/env python3
# tests/test_inspect_vm.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Tests for bin/inspect-vm.py, the datastore-direct source disk manifest."""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "inspect-vm.py"
spec = importlib.util.spec_from_file_location("inspect_vm", SCRIPT)
inspect_vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspect_vm)

GIB = 1024 ** 3

def controller(key, bus, type_name="ParaVirtualSCSIController"):
    return {"_typeName": type_name, "key": key, "busNumber": bus}

def disk(key, controller_key, unit, path, size=10 * GIB, **backing):
    backing = {"_typeName": "VirtualDiskFlatVer2BackingInfo", "fileName": path,
               "diskMode": "persistent", "sharing": "sharingNone",
               "thinProvisioned": True, "parent": None, "keyId": None, **backing}
    return {"_typeName": "VirtualDisk", "key": key, "controllerKey": controller_key,
            "unitNumber": unit, "capacityInBytes": size, "capacityInKB": size // 1024,
            "deviceInfo": {"label": f"Hard disk {key}"}, "backing": backing}

def vm_info(*devices, name="vm1"):
    return {"virtualMachines": [{"name": name, "config": {"hardware": {"device": list(devices)}}}]}

class InspectVmTests(unittest.TestCase):
    def test_orders_disks_by_controller_then_unit(self):
        info = vm_info(
            controller(1000, 0), controller(1001, 1),
            disk(2002, 1001, 0, "[ds] vm1/vm1_2.vmdk"),
            disk(2001, 1000, 1, "[ds] vm1/vm1_1.vmdk"),
            disk(2000, 1000, 0, "[ds] vm1/vm1.vmdk"))
        manifest = inspect_vm.inspect_vm(info)
        self.assertEqual([d["path"] for d in manifest["disks"]],
                         ["[ds] vm1/vm1.vmdk", "[ds] vm1/vm1_1.vmdk", "[ds] vm1/vm1_2.vmdk"])
        self.assertEqual([d["index"] for d in manifest["disks"]], [0, 1, 2])
        self.assertEqual([d["boot_candidate"] for d in manifest["disks"]], [True, False, False])
        self.assertEqual(manifest["disk_count"], 3)
        self.assertEqual(manifest["manifest_version"], 1)
        self.assertEqual(manifest["source_vm_name"], "vm1")

    def test_configured_boot_order_puts_that_disk_first(self):
        info = vm_info(
            controller(1000, 0),
            disk(2000, 1000, 0, "[ds] vm1/data.vmdk"),
            disk(2001, 1000, 1, "[ds] vm1/boot.vmdk"))
        info["virtualMachines"][0]["config"]["bootOptions"] = {"bootOrder": [
            {"deviceKey": 4000},          # a NIC entry: never a disk key
            {"deviceKey": 2001},
            {"deviceKey": 2000}]}
        manifest = inspect_vm.inspect_vm(info)
        self.assertEqual([d["path"] for d in manifest["disks"]],
                         ["[ds] vm1/boot.vmdk", "[ds] vm1/data.vmdk"])
        self.assertEqual([d["boot_candidate"] for d in manifest["disks"]], [True, False])
        self.assertEqual(manifest["boot_order_source"], "bootOptions")

    def test_boot_order_without_a_disk_keeps_controller_order(self):
        for boot_options in ({}, {"bootOrder": []}, {"bootOrder": [{"deviceKey": 4000}, {}]}):
            with self.subTest(boot_options):
                info = vm_info(controller(1000, 0), disk(2001, 1000, 1, "[ds] b.vmdk"),
                               disk(2000, 1000, 0, "[ds] a.vmdk"))
                info["virtualMachines"][0]["config"]["bootOptions"] = boot_options
                manifest = inspect_vm.inspect_vm(info)
                self.assertEqual(manifest["disks"][0]["path"], "[ds] a.vmdk")
                self.assertEqual(manifest["boot_order_source"], "controller")

    def test_manifest_passes_the_catalog_disk_order_validation(self):
        append = importlib.util.spec_from_file_location(
            "append_migration_catalog", ROOT / "bin" / "append-migration-catalog.py")
        module = importlib.util.module_from_spec(append)
        sys.path.insert(0, str(ROOT / "bin"))
        append.loader.exec_module(module)
        manifest = inspect_vm.inspect_vm(vm_info(
            controller(1000, 0), disk(2000, 1000, 0, "[ds] a.vmdk"),
            disk(2001, 1000, 1, "[ds] b.vmdk")))
        module.validate_disk_order(manifest["disks"])

    def test_path_is_the_current_file_even_with_existing_snapshots(self):
        info = vm_info(controller(1000, 0), disk(
            2000, 1000, 0, "[ds] vm1/vm1-000002.vmdk",
            parent={"fileName": "[ds] vm1/vm1-000001.vmdk",
                    "parent": {"fileName": "[ds] vm1/vm1.vmdk", "parent": None}}))
        result = inspect_vm.inspect_vm(info)["disks"][0]
        self.assertEqual(result["path"], "[ds] vm1/vm1-000002.vmdk")
        self.assertEqual(result["snapshot_chain_depth"], 2)

    def test_every_disk_records_its_snapshot_chain_depth(self):
        # check-curl-disks-vddk.yml filters on snapshot_chain_depth for every
        # manifest disk, on a fresh run and again on a curl resume, so no
        # entry may leave it out, and a flat disk must read as 0.
        manifest = inspect_vm.inspect_vm(vm_info(
            controller(1000, 0),
            disk(2000, 1000, 0, "[ds] vm1/vm1.vmdk"),
            disk(2001, 1000, 1, "[ds] vm1/vm1_1-000001.vmdk",
                 parent={"fileName": "[ds] vm1/vm1_1.vmdk", "parent": None})))
        depths = [entry.get("snapshot_chain_depth") for entry in manifest["disks"]]
        self.assertEqual(depths, [0, 1])
        self.assertTrue(all(type(depth) is int for depth in depths))

    def test_capacity_falls_back_to_kb(self):
        device = disk(2000, 1000, 0, "[ds] a.vmdk")
        del device["capacityInBytes"]
        manifest = inspect_vm.inspect_vm(vm_info(controller(1000, 0), device))
        self.assertEqual(manifest["disks"][0]["capacity_bytes"], 10 * GIB)

    def test_untyped_devices_are_recognized_by_shape(self):
        device = disk(2000, 1000, 0, "[ds] a.vmdk")
        del device["_typeName"]
        nic = {"key": 4000, "macAddress": "00:50:56:00:00:01"}
        manifest = inspect_vm.inspect_vm(vm_info(controller(1000, 0), nic, device))
        self.assertEqual(manifest["disk_count"], 1)

    def test_refuses_a_disk_without_a_key(self):
        # Without a device key the manifest cannot be built; that used to
        # surface as an uncaught KeyError instead of a clean error.
        device = disk(2000, 1000, 0, "[ds] a.vmdk")
        del device["key"]
        with self.assertRaisesRegex(ValueError, "has no key"):
            inspect_vm.inspect_vm(vm_info(controller(1000, 0), device))

    def test_refuses_a_disk_without_a_backing(self):
        device = disk(2000, 1000, 0, "[ds] a.vmdk")
        del device["backing"]
        with self.assertRaisesRegex(ValueError, "has no backing"):
            inspect_vm.inspect_vm(vm_info(controller(1000, 0), device))

    def test_refuses_unsupported_disks(self):
        cases = {
            "independent_persistent": {"diskMode": "independent_persistent"},
            "independent_nonpersistent": {"diskMode": "independent_nonpersistent"},
            "shared": {"sharing": "sharingMultiWriter"},
            "encrypted": {"keyId": {"keyId": "abc"}},
            "raw device mapping": {"_typeName": "VirtualDiskRawDiskMappingVer1BackingInfo",
                                   "deviceName": "/vmfs/devices/disks/naa.1"},
        }
        for label, backing in cases.items():
            with self.subTest(label):
                info = vm_info(controller(1000, 0), disk(2000, 1000, 0, "[ds] a.vmdk", **backing))
                with self.assertRaises(ValueError):
                    inspect_vm.inspect_vm(info)

    def test_refuses_missing_or_ambiguous_input(self):
        with self.assertRaises(ValueError):
            inspect_vm.inspect_vm({"virtualMachines": []})
        with self.assertRaises(ValueError):
            inspect_vm.inspect_vm(vm_info(controller(1000, 0)))
        with self.assertRaises(ValueError):
            both = vm_info(controller(1000, 0), disk(2000, 1000, 0, "[ds] a.vmdk"))
            both["virtualMachines"].append(copy.deepcopy(both["virtualMachines"][0]))
            inspect_vm.inspect_vm(both)
        with self.assertRaises(ValueError):
            inspect_vm.inspect_vm(vm_info(
                controller(1000, 0), disk(2000, 1000, 0, "[ds] a.vmdk"),
                disk(2001, 1000, 1, "[ds] a.vmdk")))

    def test_command_line_reads_a_file_and_reports_errors(self):
        good = vm_info(controller(1000, 0), disk(2000, 1000, 0, "[ds] a.vmdk"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "info.json"
            path.write_text(json.dumps(good))
            done = subprocess.run([sys.executable, str(SCRIPT), str(path)],
                                  capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(done.stdout)["disk_count"], 1)
            path.write_text(json.dumps({"virtualMachines": []}))
            failed = subprocess.run([sys.executable, str(SCRIPT), str(path)],
                                    capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("inspect-vm:", failed.stderr)

if __name__ == "__main__":
    unittest.main()
