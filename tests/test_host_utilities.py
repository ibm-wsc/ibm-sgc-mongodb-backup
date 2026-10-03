#!/usr/bin/env python3

# ####################################################################################
#
# Purpose : Parent test utility for hosts/host_utilities.py. Exercises the
#           SSH-based host operations in that module against real hosts, one
#           function per --check. As host_utilities.py grows new functions,
#           add a matching --check here rather than writing a new script.
#
# Usage   : test_host_utilities.py [-h]
#                                   [--host HOST]
#                                   [--uuid UUID]
#                                   [--device DEVICE]
#                                   [--mount-point MOUNT_POINT]
#                                   [--hypervisor HYPERVISOR]
#                                   [--domain DOMAIN]
#                                   [--guest-device GUEST_DEVICE]
#                                   [--check scsi-scan|find-multipath|change-fs-uuid|
#                                            highest-partition|find-scsi-disk|
#                                            find-scsi-disk-from-domain|find-scsi-uuid]
#
# NOTE    : --check scsi-scan and --check change-fs-uuid are NOT read-only.
#           scsi-scan writes to /sys/class/scsi_host/*/scan on the target
#           host to trigger a SCSI bus rescan. change-fs-uuid generates a
#           new filesystem UUID on --device (must be unmounted).
#           --check scsi-scan through find-scsi-disk require --host (the
#           guest); --check find-scsi-disk-from-domain instead requires
#           --hypervisor, --domain, and --guest-device.
#
# ####################################################################################

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hosts import host_utilities

# ---------------- Checks ----------------

def check_scsi_scan(args):
    ok = host_utilities.scan_scsi_devices(args.host)
    print(f"  {args.host} : SCSI scan {'succeeded' if ok else 'failed'}")
    return ok

def check_find_multipath(args):
    device = host_utilities.find_multipath_device_from_scsi_uuid(args.host, args.uuid)
    if not device:
        print(f"  {args.host} : no multipath device matches '{args.uuid}'")
    else:
        print(f"  {args.host} : '{args.uuid}' -> {device}")
    return device

def check_change_fs_uuid(args):
    new_uuid = host_utilities.change_filesystem_uuid(args.host, args.device)
    if not new_uuid:
        print(f"  {args.host} : UUID change failed on {args.device}")
    else:
        print(f"  {args.host} : {args.device} -> new UUID {new_uuid}")
    return new_uuid

def check_highest_partition(args):
    partition = host_utilities.highest_partition(args.host, args.device)
    if not partition:
        print(f"  {args.host} : no partitions on {args.device}")
    else:
        print(f"  {args.host} : {args.device} -> highest partition {partition}")
    return partition

def check_find_scsi_disk(args):
    device = host_utilities.find_scsi_disk_from_filesystem(args.host, args.mount_point)
    if not device:
        print(f"  {args.host} : no device found for mount point {args.mount_point}")
    else:
        print(f"  {args.host} : {args.mount_point} -> {device}")
    return device

def check_find_scsi_disk_from_domain(args):
    device = host_utilities.find_scsi_disk_from_domain_device(args.hypervisor, args.domain, args.guest_device)
    if not device:
        print(f"  {args.hypervisor} : no source found for {args.domain}'s {args.guest_device}")
    else:
        print(f"  {args.hypervisor} : {args.domain}'s {args.guest_device} -> {device}")
    return device

def check_find_scsi_uuid(args):
    uuid = host_utilities.find_scsi_uuid(args.host, args.device)
    if not uuid:
        print(f"  {args.host} : no SCSI UUID found for {args.device}")
    else:
        print(f"  {args.host} : {args.device} -> SCSI UUID {uuid}")
    return uuid

CHECKS = {
    "scsi-scan": check_scsi_scan,
    "find-multipath": check_find_multipath,
    "change-fs-uuid": check_change_fs_uuid,
    "highest-partition": check_highest_partition,
    "find-scsi-disk": check_find_scsi_disk,
    "find-scsi-disk-from-domain": check_find_scsi_disk_from_domain,
    "find-scsi-uuid": check_find_scsi_uuid,
}

# ---------------- MAIN ----------------

def main():
    parser = argparse.ArgumentParser(description="Test utility for hosts/host_utilities.py")
    parser.add_argument("--host", default=None)
    parser.add_argument("--uuid", default="*60050768138202df8800000000000465")
    parser.add_argument("--device", default="/dev/mapper/mpathn")
    parser.add_argument("--mount-point", default="/fimongo")
    parser.add_argument("--hypervisor", default="Orion0C-mdb-kvm3")
    parser.add_argument("--domain", default="rmdb7")
    parser.add_argument("--guest-device", default="vdg")
    parser.add_argument("--check", choices=sorted(CHECKS), default="scsi-scan")
    parser.add_argument("--json", action="store_true", help="Print raw result as JSON instead")
    args = parser.parse_args()

    if args.check == "find-scsi-disk-from-domain":
        print(f"\n--check {args.check} for hypervisor: {args.hypervisor}\n")
    else:
        if args.host is None:
            raise SystemExit(f"--check {args.check} requires --host")
        print(f"\n--check {args.check} for host: {args.host}\n")

    result = CHECKS[args.check](args)

    if args.json:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
