#!/usr/bin/env python3
# bin/enable-cbt.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Enable Changed Block Tracking on a vSphere VM without power-cycling it.

Sets config.changeTrackingEnabled with a ReconfigVM task, which vCenter allows
on a running VM, so the guest is not shut down. Setting the ctkEnabled
ExtraConfig keys instead does not work: vCenter ignores them and config.changeTrackingEnabled stays false.
Idempotent: a VM that already tracks changes is left alone.

Credentials come from VCENTER_HOSTNAME, VCENTER_USERNAME and VCENTER_PASSWORD
(or --vcenter). Needs pyVmomi, which community.vmware already requires.
"""

import argparse
import os
import ssl
import sys

from pyVim.connect import Disconnect, SmartConnect
from pyVim.task import WaitForTask
from pyVmomi import vim

def find_vm(content, datacenter, name):
    """Return the one VM called name under the datacenter, or raise ValueError."""
    # Datacenters may sit inside folders, so search the whole inventory for them.
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datacenter], True)
    try:
        datacenters = [dc for dc in view.view if dc.name == datacenter]
    finally:
        view.Destroy()
    if len(datacenters) != 1:
        raise ValueError(f"expected exactly one datacenter named {datacenter!r}, found {len(datacenters)}")
    view = content.viewManager.CreateContainerView(datacenters[0].vmFolder, [vim.VirtualMachine], True)
    try:
        matches = [vm for vm in view.view if vm.name == name]
    finally:
        view.Destroy()
    if len(matches) != 1:
        raise ValueError(f"expected exactly one VM named {name!r} in {datacenter!r}, found {len(matches)}")
    return matches[0]

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vcenter", default=os.environ.get("VCENTER_HOSTNAME"))
    parser.add_argument("--datacenter", required=True)
    parser.add_argument("--vm", required=True)
    parser.add_argument("--insecure", action="store_true", help="do not verify the vCenter certificate")
    args = parser.parse_args()
    user = os.environ.get("VCENTER_USERNAME")
    password = os.environ.get("VCENTER_PASSWORD")
    if not (args.vcenter and user and password):
        raise SystemExit("enable-cbt: set VCENTER_HOSTNAME (or --vcenter), VCENTER_USERNAME and VCENTER_PASSWORD")

    context = ssl.create_default_context()
    if args.insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    service = SmartConnect(host=args.vcenter, user=user, pwd=password, sslContext=context)
    try:
        vm = find_vm(service.RetrieveContent(), args.datacenter, args.vm)
        if vm.config.changeTrackingEnabled:
            print(f"CBT is already enabled on {args.vm}")
            return
        WaitForTask(vm.ReconfigVM_Task(vim.vm.ConfigSpec(changeTrackingEnabled=True)))
        if not vm.config.changeTrackingEnabled:
            raise SystemExit("enable-cbt: vCenter finished the task but CBT is still disabled")
        print(f"CBT enabled on {args.vm}")
    except (ValueError, vim.fault.VimFault) as error:
        raise SystemExit(f"enable-cbt: {error}")
    finally:
        Disconnect(service)

if __name__ == "__main__":
    sys.exit(main())
