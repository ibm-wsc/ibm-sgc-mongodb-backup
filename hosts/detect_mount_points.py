#!/usr/bin/env python3

import argparse
import json
import logging
import subprocess
import threading
import requests
from requests.auth import HTTPDigestAuth

import os
import sys

### repo root on the path so this runs standalone as well as being imported
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hosts import mongo_query



# # ---------------- DEFAULT VALUES ----------------
# # Mirrors opsman_backup.py defaults exactly

DEFAULT_POLL_INTERVAL = 10
SSH_CONNECT_TIMEOUT = 5

HEADERS = {"Content-Type": "application/json"}
logger = logging.getLogger(__name__)


# ---------------- API WRAPPER ----------------

def api(method, url, auth, payload=None):
    r = requests.request(method, url, auth=auth, headers=HEADERS, json=payload, timeout=60)
    if r.text:
        data = r.json()
        r.raise_for_status()
        return data
    return {}


# ---------------- CLUSTER DISCOVERY ----------------

def find_cluster(base_url, group_id, auth, cluster_name):
    url = f"{base_url}/api/public/v1.0/backup/third_party/group/{group_id}/clusters"
    data = api("GET", url, auth)
    for c in data.get("clusters", []):
        if c["clusterName"] == cluster_name:
            return c["clusterId"]
    raise RuntimeError(f"Cluster '{cluster_name}' not found")


def get_cluster_nodes(base_url, group_id, cluster_id, auth):
    """Return all nodes across every replica set with their id, dbPath, and memberState."""
    url = f"{base_url}/api/public/v1.0/backup/third_party/group/{group_id}/clusters/{cluster_id}"
    data = api("GET", url, auth)

    nodes = []
    for rs in data.get("replicaSets", []):
        rs_id = rs.get("id", "unknown-rs")
        for n in rs.get("nodes", []):
            nodes.append({
                "rs_id": rs_id,
                "id": n["id"],
                "db_path": n.get("dbPath", ""),
                "member_state": n.get("memberState", ""),
            })
    return nodes


# ---------------- DEVICE / UUID DETECTION ----------------

# Single SSH command that collects mount point, backing device, filesystem UUID,
# and SCSI UUID. Uses findmnt for device/mount resolution, blkid for the filesystem
# UUID, and udevadm for the SCSI/multipath UUID (DM_UUID > ID_WWN > ID_SERIAL_SHORT).
# Outputs a pipe-delimited line: MOUNT|DEVICE|FS_UUID|SCSI_UUID
_PROBE_CMD = (
    "dev=$(findmnt --noheadings --output SOURCE --target {db_path} 2>/dev/null); "
    "mnt=$(findmnt --noheadings --output TARGET --target {db_path} 2>/dev/null); "
    "uuid=$(blkid -s UUID -o value \"$dev\" 2>/dev/null); "
    "scsi_uuid=$(udevadm info --query=property --name=\"$dev\" 2>/dev/null"
    " | awk -F= '/^DM_UUID/{{print $2; exit}} /^ID_WWN/{{print $2; exit}} /^ID_SERIAL_SHORT/{{print $2; exit}}'); "
    "echo \"$mnt|$dev|$uuid|$scsi_uuid\""
)

_SSH_OPTS = [
    "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
]


def _host_from_node_id(node_id: str) -> str:
    return node_id.split(":")[0]


def probe_host(host: str, db_path: str) -> dict:
    """SSH to host and return mount_point, device, and uuid for db_path."""
    cmd = _PROBE_CMD.format(db_path=db_path)
    result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [host, cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0 or not result.stdout.strip():
        logger.warning(f"{host}: SSH failed or no output — {result.stderr.strip()}")
        return {"mount_point": "<unknown>", "device": "<unknown>", "uuid": "<unknown>", "scsi_uuid": "<unknown>"}

    parts = result.stdout.strip().split("|")
    mount     = parts[0] if len(parts) > 0 else "<unknown>"
    device    = parts[1] if len(parts) > 1 else "<unknown>"
    uuid      = parts[2] if len(parts) > 2 else "<unknown>"
    scsi_uuid = parts[3] if len(parts) > 3 else "<unknown>"

    return {
        "mount_point": mount     or "<unknown>",
        "device":      device    or "<unknown>",
        "uuid":        uuid      or "<unknown>",
        "scsi_uuid":   scsi_uuid or "<unknown>",
    }


# ---------------- HYPERVISOR DETECTION ----------------

def detect_hypervisor(host: str) -> str:
    """SSH to host and classify it as 'baremetal' or 'KVM' via `lscpu | grep Hypervisor`.

    A blank result or a line containing 'IBM' (e.g. z/VM's 'Hypervisor vendor: IBM')
    means bare metal; a line containing 'KVM' means the guest is running under KVM.
    """
    result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [host, "lscpu | grep Hypervisor"],
        capture_output=True,
        text=True,
    )

    output = result.stdout.strip()

    if not output or "IBM" in output:
        return "baremetal"
    if "KVM" in output:
        return "KVM"

    logger.warning(f"{host}: unrecognized Hypervisor line: {output!r}")
    return output


# ---------------- BLOCK DEVICE ALLOCATION (KVM) ----------------

def next_available_block_device(host: str) -> str:
    """SSH to a KVM guest and return the next unused /dev/vd[a-z] device path.

    Looks at which /dev/vd[a-z] devices already exist on the host and returns
    the path one letter past the highest one in use (e.g. vda,vdb present -> /dev/vdc).
    """
    result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [host, "ls -1 /dev/ | grep -E '^vd[a-z]$'"],
        capture_output=True,
        text=True,
    )

    letters = sorted(line.strip()[-1] for line in result.stdout.splitlines() if line.strip())
    next_letter = "a" if not letters else chr(ord(letters[-1]) + 1)

    if next_letter > "z":
        raise RuntimeError(f"{host}: no available /dev/vd[a-z] device (a-z exhausted)")

    return f"/dev/vd{next_letter}"


def detect_cluster_mount_points(nodes: list) -> list:
    """Probe all unique (host, db_path) pairs in parallel."""
    seen = set()
    work = []
    for node in nodes:
        host = _host_from_node_id(node["id"])
        key = (host, node["db_path"])
        if key not in seen:
            seen.add(key)
            work.append(node)

    results = [None] * len(work)

    def _probe(idx, node):
        host = _host_from_node_id(node["id"])
        logger.info(f"Probing {host} for dbPath {node['db_path']}")
        info = probe_host(host, node["db_path"])
        logger.info(
            f"  {host}: mount={info['mount_point']}  device={info['device']}  "
            f"uuid={info['uuid']}  scsi_uuid={info['scsi_uuid']}"
        )
        results[idx] = {
            "node_id":      node["id"],
            "rs_id":        node["rs_id"],
            "member_state": node["member_state"],
            "db_path":      node["db_path"],
            **info,
        }

    threads = [threading.Thread(target=_probe, args=(i, n)) for i, n in enumerate(work)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return results


# ---------------- MAIN ----------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Detect MongoDB data-directory mount points, devices, and UUIDs")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--group-id", default=None)
    parser.add_argument("--public-key", default=None)
    parser.add_argument("--private-key", default=None)
    parser.add_argument("--cluster-name", required=True)
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    ### credentials come from the OPSMAN_* environment variables, same as the
    ### backup and restore utilities; source opsman.cred to set them
    (
        args.base_url,
        args.group_id,
        args.public_key,
        args.private_key,
    ) = mongo_query.resolve_opsman_credentials(
        args.base_url, args.group_id, args.public_key, args.private_key
    )

    auth = HTTPDigestAuth(args.public_key, args.private_key)

    cluster_id = find_cluster(args.base_url, args.group_id, auth, args.cluster_name)
    logger.info(f"Resolved cluster '{args.cluster_name}' -> {cluster_id}")

    nodes = get_cluster_nodes(args.base_url, args.group_id, cluster_id, auth)
    logger.info(f"Found {len(nodes)} node(s) across {len({n['rs_id'] for n in nodes})} replica set(s)")

    mount_info = detect_cluster_mount_points(nodes)

    if args.json:
        print(json.dumps(mount_info, indent=2))
    else:
        print(f"\nMount info for cluster '{args.cluster_name}':\n")
        for entry in mount_info:
            print(f"  Node        : {entry['node_id']}")
            print(f"  ReplicaSet  : {entry['rs_id']}")
            print(f"  State       : {entry['member_state']}")
            print(f"  dbPath      : {entry['db_path']}")
            print(f"  Mount Point : {entry['mount_point']}")
            print(f"  Device      : {entry['device']}")
            print(f"  FS UUID     : {entry['uuid']}")
            print(f"  SCSI UUID   : {entry['scsi_uuid']}")
            print()

    return mount_info


if __name__ == "__main__":
    main()
