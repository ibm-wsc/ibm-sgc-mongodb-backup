#!/usr/bin/env python3

# ####################################################################################
#
# Purpose : Parent test utility for hosts/detect_mount_points.py. Exercises the
#           SSH-based host probes in that module against real hosts, one function
#           per --check. As detect_mount_points.py grows new functions, add a
#           matching --check here rather than writing a new script.
#
# Usage   : test_detect_mount_points.py [-h]
#                                        --hosts host1,host2,...
#                                        [--check hypervisor]
#
# ####################################################################################

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hosts import detect_mount_points

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------------- Checks ----------------

def check_hypervisor(args):
    results = {}
    for host in args.hosts:
        hv = detect_mount_points.detect_hypervisor(host)
        print(f"  {host:15s} : {hv}")
        results[host] = hv
    return results

def check_next_block_device(args):
    results = {}
    for host in args.hosts:
        dev = detect_mount_points.next_available_block_device(host)
        print(f"  {host:15s} : {dev}")
        results[host] = dev
    return results

CHECKS = {
    "hypervisor": check_hypervisor,
    "next-block-device": check_next_block_device,
}

# ---------------- MAIN ----------------

def main():
    parser = argparse.ArgumentParser(description="Test utility for hosts/detect_mount_points.py")
    parser.add_argument(
        "--hosts",
        required=True,
        metavar="host1,host2,...",
        help="Comma-separated list of hosts to probe.",
    )
    parser.add_argument("--check", choices=sorted(CHECKS), default="hypervisor")
    parser.add_argument("--json", action="store_true", help="Print raw result as JSON instead")
    args = parser.parse_args()
    args.hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]

    print(f"\n--check {args.check} for hosts: {', '.join(args.hosts)}\n")
    result = CHECKS[args.check](args)

    if args.json:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
