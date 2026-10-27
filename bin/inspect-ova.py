#!/usr/bin/env python3
# bin/inspect-ova.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Inspect an OVA or standalone OVF and emit a disk manifest."""

import argparse
import json
import re
import tarfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

UNIT_FACTORS = {
    "byte": Decimal(1), "bytes": Decimal(1), "kb": Decimal(1000),
    "mb": Decimal(1000) ** 2, "gb": Decimal(1000) ** 3,
    "tb": Decimal(1000) ** 4, "kib": Decimal(1024),
    "mib": Decimal(1024) ** 2, "gib": Decimal(1024) ** 3,
    "tib": Decimal(1024) ** 4,
}

def local_name(tag):
    return tag.rsplit("}", 1)[-1]

def attr(element, name, default=None):
    for key, value in element.attrib.items():
        if local_name(key) == name:
            return value
    return default

def child_text(element, name, default=None):
    for child in element:
        if local_name(child.tag) == name:
            return (child.text or "").strip()
    return default

def capacity_bytes(value, units):
    if value is None:
        return None
    units = units or "byte"
    power_match = re.fullmatch(r"\s*byte\s*\*\s*2\s*\^\s*(\d+)\s*", units, re.IGNORECASE)
    if power_match:
        factor = Decimal(2) ** int(power_match.group(1))
    else:
        factor = UNIT_FACTORS.get(re.sub(r"[^A-Za-z]", "", units).lower())
    if factor is None:
        raise ValueError(f"unsupported OVF capacity units: {units}")
    try:
        result = Decimal(value) * factor
    except InvalidOperation as error:
        raise ValueError(f"invalid OVF capacity: {value}") from error
    if result != result.to_integral_value():
        raise ValueError(f"non-integral OVF capacity in bytes: {value} {units}")
    return int(result)

def _inspect_descriptor(root, artifact_type, artifact_filename, ovf_filename,
                        file_validator=None):
    # One walk of the descriptor, bucketed by element name: File entries must
    # be known before the Disks that reference them, and the Disks before the
    # Items that attach them, but the tree only has to be traversed once.
    elements = {"File": [], "Disk": [], "Item": []}
    for element in root.iter():
        bucket = elements.get(local_name(element.tag))
        if bucket is not None:
            bucket.append(element)

    files = {}
    for element in elements["File"]:
        file_id = attr(element, "id")
        href = attr(element, "href")
        if not file_id or not href:
            raise ValueError("OVF File entry lacks id or href")
        size = attr(element, "size")
        files[file_id] = {
            "path": href,
            "size_bytes": int(size) if size is not None else None,
            "compression": attr(element, "compression"),
        }

    disks = {}
    for element in elements["Disk"]:
        disk_id = attr(element, "diskId")
        file_ref = attr(element, "fileRef")
        if not disk_id or not file_ref:
            raise ValueError("OVF Disk entry lacks diskId or fileRef")
        if file_ref not in files:
            raise ValueError(f"OVF disk {disk_id} refers to unknown file {file_ref}")
        disks[disk_id] = {
            "disk_id": disk_id, "file_ref": file_ref,
            "path": files[file_ref]["path"],
            "file_size_bytes": files[file_ref]["size_bytes"],
            "capacity_bytes": capacity_bytes(attr(element, "capacity"), attr(element, "capacityAllocationUnits", "byte")),
            "populated_size_bytes": capacity_bytes(attr(element, "populatedSize"), attr(element, "populatedSizeAllocationUnits", "byte")),
            "format": attr(element, "format"),
            # Where the descriptor lists this disk. AWS VM Import chooses its
            # root volume from the package, not from our index, so the two
            # orders must be checked against each other.
            "ovf_position": len(disks),
        }

    # Disk controllers (IDE 5, SCSI 6, SATA 20), keyed by InstanceID. A disk's
    # boot order is its controller's bus, then its unit on that controller;
    # the disk Item's own InstanceID only reflects the order vSphere listed it.
    controllers = {}
    for element in elements["Item"]:
        if child_text(element, "ResourceType") in {"5", "6", "20"}:
            controllers[child_text(element, "InstanceID")] = child_text(element, "Address")

    attachments = []
    for element in elements["Item"]:
        resource_type = child_text(element, "ResourceType")
        host_resource = child_text(element, "HostResource") or ""
        match = re.search(r"/disk/([^/]+)", host_resource)
        if resource_type != "17" or not match:
            continue
        disk_id = match.group(1)
        if disk_id not in disks:
            raise ValueError(f"virtual disk attachment refers to unknown disk {disk_id}")
        attachment = {
            "disk_id": disk_id, "instance_id": child_text(element, "InstanceID"),
            "element_name": child_text(element, "ElementName"),
            "address": child_text(element, "Address"),
            "address_on_parent": child_text(element, "AddressOnParent"),
            "parent": child_text(element, "Parent"), "host_resource": host_resource,
        }
        disks[disk_id]["attachment"] = attachment
        attachments.append(attachment)

    ordered_disks = list(disks.values())
    for disk in ordered_disks:
        disk.setdefault("attachment", None)
    def number(value):
        return int(value) if (value or "").isdigit() else 2**31

    def boot_order(item):
        attachment = item.get("attachment")
        if not attachment:
            return (2**31, 2**31, 2**31, item["disk_id"])
        parent = attachment.get("parent")
        return (
            number(controllers.get(parent)) if parent in controllers else 2**31,
            number(parent),
            number(attachment.get("address_on_parent")),
            item["disk_id"],
        )

    ordered_disks.sort(key=boot_order)
    for index, disk in enumerate(ordered_disks):
        disk["index"] = index
        disk["boot_candidate"] = index == 0
        if file_validator is not None:
            file_validator(disk, files[disk["file_ref"]])

    result = {
        "manifest_version": 1, "artifact_type": artifact_type,
        "ovf_filename": ovf_filename, "disk_count": len(ordered_disks),
        "disks": ordered_disks, "virtual_disk_attachments": attachments,
    }
    if artifact_type == "ova":
        result["ova_filename"] = Path(artifact_filename).name
    return result

def inspect_ova(path):
    with tarfile.open(path, "r:*") as archive:
        ovf_members = [member for member in archive.getmembers() if member.name.lower().endswith(".ovf")]
        if len(ovf_members) != 1:
            raise ValueError(f"expected exactly one OVF descriptor, found {len(ovf_members)}")
        ovf_member = ovf_members[0]
        stream = archive.extractfile(ovf_member)
        if stream is None:
            raise ValueError(f"could not read OVF descriptor {ovf_member.name}")
        return _inspect_descriptor(ET.parse(stream).getroot(), "ova", path, ovf_member.name)

def inspect_ovf(path):
    path = Path(path)
    base_dir = path.parent.resolve()

    def validate_file(disk, file_entry):
        href = file_entry["path"]
        relative = Path(href)
        parsed = urlparse(href)
        if (not href or parsed.scheme or parsed.netloc or parsed.query or
                parsed.fragment or "\\" in href or relative.is_absolute() or ".." in relative.parts):
            raise ValueError(f"unsafe OVF disk reference: {href}")
        compression = (file_entry.get("compression") or "").lower()
        if compression and compression != "none":
            raise ValueError(
                f"compressed OVF disk is unsupported ({compression}): {href}")
        if href.lower().endswith(".gz") or "gzip" in (disk.get("format") or "").lower():
            raise ValueError(f"gzip-compressed OVF disk is unsupported: {href}")
        disk_path = (base_dir / relative).resolve()
        try:
            disk_path.relative_to(base_dir)
        except ValueError as error:
            raise ValueError(f"OVF disk escapes package directory: {href}") from error
        if not disk_path.is_file():
            raise ValueError(f"OVF disk is missing: {href}")
        actual_size = disk_path.stat().st_size
        if (file_entry["size_bytes"] is not None and
                actual_size != file_entry["size_bytes"]):
            raise ValueError(f"OVF disk size mismatch for {href}: expected {file_entry['size_bytes']}, found {actual_size}")

    return _inspect_descriptor(ET.parse(path).getroot(), "ovf", path, path.name, validate_file)

def main():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--ova", type=Path)
    source.add_argument("--ovf", type=Path)
    args = parser.parse_args()
    path = args.ova or args.ovf
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f"artifact is missing or empty: {path}")
    try:
        inspector = inspect_ova if args.ova else inspect_ovf
        print(json.dumps(inspector(path), indent=2, sort_keys=False))
    except (OSError, tarfile.TarError, ET.ParseError, ValueError, KeyError) as error:
        raise SystemExit(f"inspect-ova: {error}")

if __name__ == "__main__":
    main()
