#!/usr/bin/env python3

# ####################################################################################
#
# Purpose : Show a MongoDB cluster's topology (by shard/replica set) alongside
#           the storage stack backing each node: dbPath, filesystem UUID and
#           block device, hypervisor and hypervisor-side SCSI device (for KVM
#           guests), and the FlashSystem volume/volume group presenting that
#           storage (and, for any replicated clones of that volume group,
#           the replication policy and its target location).
#
#           Built entirely on functions already in hosts/ and ibmflashsystem/:
#             - hosts.mongo_query.list_cluster_nodes
#             - hosts.detect_mount_points.probe_host
#             - hosts.host_utilities.find_scsi_uuid, find_scsi_disk_from_domain_device
#             - ibmflashsystem.flashsystem_utilities.getFlashSystemToken,
#               getVolumeIDfromUUID, getVolumeGroupFromVolumeID,
#               listDependentVolumeGroups, getReplicationPolicyTargetLocation
#
#           make_infrastructure_map() prints the tree to stdout and also
#           returns the same data as a JSON-serializable dict.
#
# Usage   : opsman_infrastructure_map.py [-h]
#                                       --cluster-name CLUSTER_NAME
#                                       [--nodes host:port,host:port,...]
#                                       [--base-url OPSMANAGER_URL]
#                                       [--group-id GROUP_ID]
#                                       [--public-key PUBLIC_KEY]
#                                       [--private-key PRIVATE_KEY]
#
# ####################################################################################

import argparse
import importlib.machinery
import importlib.util
import json
import os
import sys
from collections import defaultdict

from requests.auth import HTTPDigestAuth

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import flash_credentials as flashsystems
from hosts import mongo_query, detect_mount_points, host_utilities
from ibmflashsystem import flashsystem_utilities

# ---------------- Credentials ----------------

def resolve_credentials(args):
    """Fill in any Ops Manager credential not given on the command line from
    the OPSMAN_* environment variables (source opsman.cred to set them)."""
    (
        args.base_url,
        args.group_id,
        args.public_key,
        args.private_key,
    ) = mongo_query.resolve_opsman_credentials(
        args.base_url, args.group_id, args.public_key, args.private_key
    )

def mapping_host_object(infra, host):
    """Return the FlashSystem host object a volume should be mapped to so that
    `host` can see it.

    A bare-metal node maps to its own host object. A virtualised node never
    gets a direct mapping: the volume is attached to its hypervisor, which then
    presents it to the guest, so the hypervisor's host object is used instead.

    Returns None when the host, its hypervisor, or the relevant host object is
    missing from infrastructure.map.
    """
    guest = infra.guests.get(host)
    if guest is None:
        return None

    hypervisor = guest.get("hypervisor", "none")

    if hypervisor and hypervisor.lower() != "none":
        hv = infra.hypervisors.get(hypervisor)
        if hv is None:
            return None
        return hv.get("hostobject")

    return guest.get("hostobject")


def load_infrastructure_credentials():
    cred_path = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "infrastructure.map")
    )
    if not os.path.isfile(cred_path):
        raise SystemExit(f"Missing infrastructure credentials file: {cred_path}")
    loader = importlib.machinery.SourceFileLoader("infrastructure_cred", cred_path)
    spec = importlib.util.spec_from_file_location("infrastructure_cred", cred_path, loader=loader)
    cred = importlib.util.module_from_spec(spec)
    loader.exec_module(cred)
    return cred

# ---------------- TREE RENDERING ----------------

def _print_tree(label, children, prefix="", is_last=True, is_root=False):
    """Print (label, children) recursively as a box-drawing tree, where
    children is a list of (label, children) tuples (leaves have children=[])."""
    if is_root:
        print(label)
    else:
        print(f"{prefix}{'└── ' if is_last else '├── '}{label}")

    child_prefix = prefix if is_root else prefix + ("    " if is_last else "│   ")
    for i, (child_label, child_children) in enumerate(children):
        _print_tree(child_label, child_children, child_prefix, i == len(children) - 1)

# ---------------- HELPERS ----------------

def _domain_device_name(block_device):
    """Reduce '/dev/vdg1' or '/dev/mapper/mpathn1' to the bare device/alias
    name a hypervisor's virsh domblklist Target column uses (e.g. 'vdg'),
    by stripping the directory and any trailing partition digits."""
    name = block_device.rsplit("/", 1)[-1]
    while name and name[-1].isdigit():
        name = name[:-1]
    return name

def _find_flashsystem_volume(scsi_uuid, tokens):
    """Try every configured FlashSystem for a volume matching scsi_uuid.
    Returns (system, volume_id) or (None, None) if no system has it."""
    for entry in flashsystems.FlashSystems:
        system = entry["system"]
        if system not in tokens:
            tokens[system] = flashsystem_utilities.getFlashSystemToken(
                entry["system"], entry["user"], entry["password"]
            )
        volume_id = flashsystem_utilities.getVolumeIDfromUUID(system, tokens[system], scsi_uuid)
        if volume_id:
            return system, volume_id
    return None, None

# ---------------- REPORT ----------------

def _build_node_data(node, infra, fs_tokens):
    """Resolve one cluster node's storage chain and return it as a plain,
    JSON-serializable dict. Fields that can't be resolved are left None
    (or [] for replicated_clones) rather than raising."""
    host = node["id"].split(":")[0]
    db_path = node["dbPath"]

    data = {
        "id": node["id"],
        "shard": node.get("rs_id"),
        "member_state": node["memberState"],
        "db_path": db_path,
        "filesystem_uuid": None,
        "block_device": None,
        "hypervisor": None,
        "hypervisor_scsi": None,
        "scsi_uuid": None,
        "flashsystem": None,
        "volume_id": None,
        "volume_group": None,
        "replicated_clones": [],
    }

    info = detect_mount_points.probe_host(host, db_path)
    block_device = info["device"]
    fs_uuid = info["uuid"]

    data["filesystem_uuid"] = fs_uuid or None
    data["block_device"] = block_device or None

    if block_device in ("<unknown>", ""):
        return data

    guest = infra.guests.get(host)
    hypervisor = guest.get("hypervisor", "none") if guest is not None else None
    data["hypervisor"] = hypervisor

    # Virtio guest block devices carry no SCSI/WWN identity of their own
    # (confirmed via udevadm — no DM_UUID/ID_WWN/ID_SERIAL_SHORT on a KVM
    # guest's /dev/vdgN); the real SCSI identity only exists on the
    # hypervisor's host-side device, so the UUID lookup must run there
    # instead of against the guest for virtualized nodes.
    scsi_uuid_host, scsi_uuid_device = host, block_device

    if hypervisor is not None and hypervisor.lower() != "none":
        domain_device = _domain_device_name(block_device)
        hv_source = host_utilities.find_scsi_disk_from_domain_device(hypervisor, host, domain_device)
        data["hypervisor_scsi"] = hv_source or None
        if hv_source:
            scsi_uuid_host, scsi_uuid_device = hypervisor, hv_source

    scsi_uuid = host_utilities.find_scsi_uuid(scsi_uuid_host, scsi_uuid_device)
    data["scsi_uuid"] = scsi_uuid or None
    if not scsi_uuid:
        return data

    system, volume_id = _find_flashsystem_volume(scsi_uuid, fs_tokens)
    data["flashsystem"] = system
    data["volume_id"] = volume_id
    if not system:
        return data

    volume_group = flashsystem_utilities.getVolumeGroupFromVolumeID(system, fs_tokens[system], volume_id)
    data["volume_group"] = volume_group or None
    if not volume_group:
        return data

    dependents = flashsystem_utilities.listDependentVolumeGroups(system, fs_tokens[system], volume_group)
    if dependents:
        for dep_name, policy_name in dependents:
            target = None
            if policy_name:
                target = flashsystem_utilities.getReplicationPolicyTargetLocation(
                    system, fs_tokens[system], policy_name
                ) or None
            data["replicated_clones"].append(
                {"name": dep_name, "policy": policy_name or None, "target": target}
            )

    return data


def _node_data_to_tree(data):
    """Return (label, children) rendering one node's data dict as a tree,
    matching the same display previously produced inline."""
    children = [
        (f"dbPath: {data['db_path']}", []),
        (f"Filesystem UUID: {data['filesystem_uuid'] or '<unknown>'}", []),
        (f"Block device: {data['block_device'] or '<unknown>'}", []),
    ]

    if data["block_device"] is None:
        children.append(("(no block device resolved — skipping storage lookups)", []))
        return f"{data['id']} ({data['member_state']})", children

    hypervisor = data["hypervisor"]
    if hypervisor is None:
        children.append((f"Hypervisor: unknown ({data['id'].split(':')[0]} not in infrastructure.map)", []))
    elif hypervisor.lower() != "none":
        hv_children = [(f"Hypervisor SCSI: {data['hypervisor_scsi'] or 'unavailable'}", [])]
        children.append((f"Hypervisor: {hypervisor}", hv_children))
    else:
        children.append(("Hypervisor: none (bare metal)", []))

    if not data["scsi_uuid"]:
        children.append(("FlashSystem: unavailable (no SCSI UUID resolved)", []))
        return f"{data['id']} ({data['member_state']})", children

    if not data["flashsystem"]:
        children.append((f"FlashSystem: not found on any configured system (UUID {data['scsi_uuid']})", []))
        return f"{data['id']} ({data['member_state']})", children

    fs_children = [(f"Volume ID: {data['volume_id']}", [])]

    if not data["volume_group"]:
        fs_children.append(("Volume Group: unavailable", []))
    else:
        if not data["replicated_clones"]:
            vg_children = [("Replicated: no", [])]
        else:
            vg_children = []
            for clone in data["replicated_clones"]:
                if not clone["policy"]:
                    vg_children.append((f"{clone['name']}: replication policy unavailable", []))
                else:
                    vg_children.append(
                        (f"{clone['name']}: policy '{clone['policy']}' -> target {clone['target'] or 'unknown'}", [])
                    )
        fs_children.append((f"Volume Group: {data['volume_group']}", vg_children))

    children.append((f"FlashSystem: {data['flashsystem']}", fs_children))

    return f"{data['id']} ({data['member_state']})", children


def make_infrastructure_map(base_url, group_id, auth, cluster_name, nodes=None):
    """Print a text tree of `cluster_name`'s topology (by shard/replica set),
    with each node's dbPath, filesystem UUID and block device, hypervisor
    and hypervisor-side SCSI device (if any), and the FlashSystem
    volume/volume group presenting that storage.

    `nodes`, if given, is a list of node ids (e.g. 'mdb7:37017') to restrict
    the map to; by default every node in the cluster is shown.

    Returns the same topology as a JSON-serializable dict:
    {"cluster": cluster_name, "shards": [{"rs_id": ..., "nodes": [node_data, ...]}, ...]}
    where each node_data is the dict built by _build_node_data."""
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    environment = {"cluster": cluster_name, "shards": []}

    all_nodes = mongo_query.list_cluster_nodes(base_url, group_id, auth, cluster_name)
    if not all_nodes:
        print(f"No nodes found for cluster '{cluster_name}'")
        return environment

    if nodes is None:
        selected_nodes = all_nodes
    else:
        wanted = set(nodes)
        selected_nodes = [n for n in all_nodes if n["id"] in wanted]
        missing = wanted - {n["id"] for n in selected_nodes}
        if missing:
            print(f"Warning: node(s) not found in cluster '{cluster_name}': {', '.join(sorted(missing))}")
        if not selected_nodes:
            print(f"No matching nodes found for cluster '{cluster_name}'")
            return environment

    infra = load_infrastructure_credentials()
    fs_tokens = {}

    groups = defaultdict(list)
    for n in selected_nodes:
        groups[n["rs_id"]].append(n)

    shard_nodes = []
    for rs_id in sorted(groups):
        node_data = [
            _build_node_data(node, infra, fs_tokens)
            for node in sorted(groups[rs_id], key=lambda n: n["id"])
        ]
        environment["shards"].append({"rs_id": rs_id, "nodes": node_data})
        shard_nodes.append((f"Shard / Replica Set: {rs_id}", [_node_data_to_tree(d) for d in node_data]))

    print()
    _print_tree(f"Cluster: {cluster_name}", shard_nodes, is_root=True)

    return environment

# ---------------- MAIN ----------------

def main():
    parser = argparse.ArgumentParser(description="Show a MongoDB cluster's infrastructure map")
    parser.add_argument("--cluster-name", required=True)
    parser.add_argument(
        "--nodes",
        default=None,
        metavar="host:port,host:port,...",
        help="Comma-separated list of node ids to restrict the map to; omit for every node.",
    )
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--group-id", default=None)
    parser.add_argument("--public-key", default=None)
    parser.add_argument("--private-key", default=None)
    parser.add_argument(
        "--json-out",
        default=None,
        metavar="PATH",
        help="If given, write the mapped environment as JSON to this path.",
    )
    args = parser.parse_args()
    resolve_credentials(args)

    auth = HTTPDigestAuth(args.public_key, args.private_key)

    node_filter = [n.strip() for n in args.nodes.split(",")] if args.nodes else None

    environment = make_infrastructure_map(
        args.base_url, args.group_id, auth, args.cluster_name, nodes=node_filter
    )

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(environment, f, indent=2)
        print(f"\nWrote JSON environment to {args.json_out}")


if __name__ == "__main__":
    main()
