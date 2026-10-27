#!/usr/bin/env python3
# bin/inspect-vm.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Build a disk manifest from `govc vm.info -json` of a source VM.

The datastore-direct migration reads the source VM's own disks, so there is no
OVF to inspect. The manifest has the same shape inspect-ova.py emits (disks
numbered 0..n-1 in boot order, only disk 0 a boot candidate), which lets the
validation, import, and catalog stages run on it unchanged. Boot order is the
VM's configured one (config.bootOptions.bootOrder) when it names a disk, and
otherwise controller bus then unit, the firmware default; `boot_order_source`
records which.

Run this on `govc vm.info -json` captured BEFORE the migration snapshot is
created. Each disk's `path` is then the file that the snapshot freezes; after
the snapshot, backing.fileName is the new delta instead. `snapshot_chain_depth`
records how many parent files that file has (0 for a disk with no snapshots);
the curl backend refuses a non-zero depth because it reads each disk as one
flat file (VixDiskLib reads the chain itself).

Disks that a snapshot does not freeze or that cannot be copied as a plain
VMDK are refused rather than read live: independent disks, shared (multi-
writer) disks, raw device mappings, and encrypted disks.
"""

import argparse
import json
import sys
from pathlib import Path

# Sorts a disk with no usable address after every addressed one.
UNADDRESSED = 2 ** 31

def disk_devices(devices):
    """Return the virtual disks among a VM's hardware devices.

    A device is a disk when it names itself one, or (for JSON that omits
    _typeName) when it carries both a backing and a capacity. The fields below
    are then read unconditionally, so a disk missing its device key or backing
    is refused here instead of surfacing later as an uncaught KeyError.
    """
    disks = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        type_name = device.get("_typeName")
        if type_name is not None:
            if type_name != "VirtualDisk":
                continue
        elif not (isinstance(device.get("backing"), dict) and (
                "capacityInBytes" in device or "capacityInKB" in device)):
            continue
        if device.get("key") is None:
            raise ValueError("a virtual disk device has no key")
        if not isinstance(device.get("backing"), dict):
            raise ValueError(f"disk {device['key']} has no backing")
        disks.append(device)
    return disks

def capacity_bytes(device):
    value = device.get("capacityInBytes")
    if value is None and device.get("capacityInKB") is not None:
        value = int(device["capacityInKB"]) * 1024
    if value is None or int(value) <= 0:
        raise ValueError(f"disk {device.get('key')} has no usable capacity")
    return int(value)

def check_supported(device, label):
    """Raise ValueError unless a snapshot freezes this disk and it is a plain VMDK."""
    backing = device.get("backing") or {}
    type_name = backing.get("_typeName") or ""
    if "RawDisk" in type_name or backing.get("deviceName") or backing.get("compatibilityMode"):
        raise ValueError(f"{label} is a raw device mapping; not supported")
    path = backing.get("fileName") or ""
    if not path.lower().endswith(".vmdk"):
        raise ValueError(f"{label} has no VMDK file backing ({path or 'no fileName'})")
    mode = backing.get("diskMode") or "persistent"
    if mode != "persistent":
        raise ValueError(
            f"{label} is {mode}; a snapshot does not freeze it, so it cannot be read consistently")
    sharing = backing.get("sharing") or "sharingNone"
    if sharing != "sharingNone":
        raise ValueError(f"{label} is shared ({sharing}); not supported")
    if backing.get("keyId"):
        raise ValueError(f"{label} is encrypted; not supported")

def chain_depth(backing):
    depth = 0
    parent = backing.get("parent")
    while isinstance(parent, dict):
        depth += 1
        parent = parent.get("parent")
    return depth

def configured_boot_disk(machine, disk_keys):
    """Return the device key of the first disk in the VM's boot order, or None.

    config.bootOptions.bootOrder lists bootable devices in order. Disk entries
    carry the disk's deviceKey (so do NIC entries, which never match a disk
    key). An empty or absent list means the firmware default: the first disk
    by controller and unit, which is the order the manifest already uses.
    """
    boot_order = ((machine.get("config") or {}).get("bootOptions") or {}).get("bootOrder") or []
    for entry in boot_order:
        key = entry.get("deviceKey") if isinstance(entry, dict) else None
        if key in disk_keys:
            return key
    return None

def inspect_vm(info):
    machines = info.get("virtualMachines") or []
    if len(machines) != 1:
        raise ValueError(f"expected exactly one VM, found {len(machines)}")
    machine = machines[0]
    devices = ((machine.get("config") or {}).get("hardware") or {}).get("device") or []

    controllers = {
        device["key"]: device["busNumber"]
        for device in devices
        if "key" in device and device.get("busNumber") is not None
    }

    disks = []
    for device in disk_devices(devices):
        label = f"disk {device.get('key')} ({(device.get('deviceInfo') or {}).get('label', 'unlabeled')})"
        check_supported(device, label)
        controller_key = device.get("controllerKey")
        unit = device.get("unitNumber")
        bus = controllers.get(controller_key)
        backing = device["backing"]
        disks.append({
            "disk_id": str(device["key"]),
            "path": backing["fileName"],
            "file_size_bytes": None,
            "capacity_bytes": capacity_bytes(device),
            "populated_size_bytes": None,
            "format": "vmdk",
            "thin_provisioned": bool(backing.get("thinProvisioned")),
            "snapshot_chain_depth": chain_depth(backing),
            "attachment": {
                "device_key": device["key"],
                "parent": None if controller_key is None else str(controller_key),
                "address": None if bus is None else str(bus),
                "address_on_parent": None if unit is None else str(unit),
            },
            "_order": (
                UNADDRESSED if bus is None else int(bus),
                UNADDRESSED if controller_key is None else int(controller_key),
                UNADDRESSED if unit is None else int(unit),
                int(device["key"]),
            ),
        })

    if not disks:
        raise ValueError("the VM has no virtual disks")
    paths = [disk["path"] for disk in disks]
    if len(set(paths)) != len(paths):
        raise ValueError("two virtual disks share one backing file")

    disks.sort(key=lambda disk: disk["_order"])
    boot_order_source = "controller"
    boot_key = configured_boot_disk(machine, {disk["attachment"]["device_key"] for disk in disks})
    if boot_key is not None:
        boot_order_source = "bootOptions"
        disks.sort(key=lambda disk: disk["attachment"]["device_key"] != boot_key)
    for index, disk in enumerate(disks):
        del disk["_order"]
        disk["index"] = index
        disk["boot_candidate"] = index == 0

    return {
        "manifest_version": 1,
        "artifact_type": "datastore",
        "source_vm_name": machine.get("name"),
        "disk_count": len(disks),
        "boot_order_source": boot_order_source,
        "disks": disks,
        "virtual_disk_attachments": [disk["attachment"] for disk in disks],
    }

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("json_file", nargs="?", type=Path,
                        help="govc vm.info -json output (default: stdin)")
    args = parser.parse_args()
    try:
        text = args.json_file.read_text() if args.json_file else sys.stdin.read()
        print(json.dumps(inspect_vm(json.loads(text)), indent=2, sort_keys=False))
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f"inspect-vm: {error}")

if __name__ == "__main__":
    main()
