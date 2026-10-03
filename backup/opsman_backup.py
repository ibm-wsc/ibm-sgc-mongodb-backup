#!/usr/bin/env python3

# ####################################################################################
#
# Purpose : Implements an automated MongoDB backup workflow using MongoDB Ops Manager
#           third-party backup APIs in conjunction with IBM FlashSystem Snapshot /
#           Safeguarded Copy.
#
#           The script performs the following:
#             - Discovers MongoDB cluster by name
#             - Selects one snapshot-eligible node per replica set
#               (supports single node, replica set, and sharded clusters)
#             - Initiates and manages Ops Manager third-party snapshots
#             - Pauses for IBM FlashSystem snapshot coordination
#             - Captures and stores snapshot metadata for restore workflows
#
# Author  : Jai Waghela
#
# Date
# Written : Tue Jan 06 2026
#
# Version : 0.1 : Initial version
#           0.2 : Added sharded cluster support (one node per replica set)
#                 Simplified opTime comparison logic
#                 Introduced per-run directory structure for logs and metadata
#           0.3 : Integrated IBM FlashSystem snapshot tagging and metadata capture manually
#           0.4 : Automated IBM FlashSystem snapshot creation via API
#           0.5 : Updated metadata capture to include IBM FlashSystem snapshot details
#           1.0 : Milestone version to support sharded and replca sets
#           1.1 : Preserved UUID in backup to validate the restore points
#           1.2 : Ops Manager credentials now come from the OPSMAN_* environment
#                 variables (source opsman.cred); CLI flags still take precedence
#           1.3 : De-looped node and storage calls to be discrete function calls
#           2.0 : Shard-based snapshots. FlashSystem snapshots are named
#                 {shard}_{snapshotId} rather than after the volume group, so a
#                 restore can tell which shard's data a snapshot holds, and the
#                 shard name is recorded in the metadata.
#                 Replication is driven by the infrastructure map rather than the
#                 static volume group map: dependent volume groups are discovered
#                 per source volume group, every replicated clone is refreshed
#                 from the new snapshot, and the remote snapshot is named to
#                 match its local counterpart. Remote snapshots are only taken
#                 for clones that actually refreshed, so a stale replica can no
#                 longer be captured as a current restore point.
#                 Adds --review to map the cluster and flag any shard not backed
#                 by a volume group with a replicated dependent.
#
# Usage   : opsmgr_snapshot.py [-h]
#                              --cluster-name CLUSTER_NAME
#                              [--base-url OPSMANAGER_URL]
#                              [--group-id GROUP_ID]
#                              [--public-key PUBLIC_KEY]
#                              [--private-key PRIVATE_KEY]
#                              [--poll-interval SECONDS]
#                              [--nodes host:port,host:port,...]
#                              [--save-node-data all|node1,node2,...]
#                              [--review]
#
#           --review maps the cluster and flags any shard that is not backed
#           by a volume group with a replicated dependent, then exits without
#           taking a backup.
#
# ####################################################################################

# ####################################################################################
#
#                           **** DISCLAIMER ****
#
# This script is provided as a reference implementation to demonstrate a MongoDB
# backup workflow using Ops Manager third-party APIs and IBM FlashSystem Snapshot /
# Safeguarded Copy in a controlled environment.
#
# There is no official support provided by IBM for this script.
#
# Under no circumstances should this script be deployed directly into a Production
# environment without thorough testing, validation, and adaptation to local standards.
#
# Users are encouraged to develop and maintain their own operational tooling based
# on this sample.
#
# ####################################################################################

import time
import json
import argparse
import logging
import os
import sys
import subprocess
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
import requests
from requests.auth import HTTPDigestAuth

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import flash_credentials
import ibmflashsystem.flashsystem_backup as flashsystem_backup
import ibmflashsystem.flashsystem_utilities as flashsystem_utilities
from hosts.detect_mount_points import get_cluster_nodes, detect_cluster_mount_points
from hosts import mongo_query
from hosts import opsman_infrastructure_map as infrastructure_map



# ---------------- Credentials and Base Parameters ----------------

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

DEFAULT_POLL_INTERVAL = 10

### snapshot states that never progress, so waiting on them is pointless
TERMINAL_SNAPSHOT_STATES = {"FAILED", "CANCELLED", "EXPIRED"}

HEADERS = {"Content-Type": "application/json"}
logger = None

# ---------------- SIMPLE HELPERS ----------------

def safe_name(name):
    return name.replace(" ", "_").replace("/", "_").replace(":", "_")

def utc_ts():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

# ---------------- RUN DIRECTORY ----------------

def create_run_dir(cluster_name):
    run_dir = f"runs/{safe_name(cluster_name)}_{utc_ts()}"
    os.makedirs(run_dir, exist_ok=True)
    return run_dir

# ---------------- LOGGING ----------------

def setup_logging(log_file):
    logger = logging.getLogger("opsmgr_snapshot")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=3)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)

    return logger

def rename_log_file(old_path, run_dir, snapshot_id):
    logging.shutdown()
    new_path = os.path.join(run_dir, f"snapshot_{snapshot_id}.log")
    os.rename(old_path, new_path)
    return new_path

# ---------------- API WRAPPER ----------------

def api(method, url, auth, payload=None):
    logger.debug(f"API {method} {url}")
    if payload:
        logger.debug(json.dumps(payload, indent=2))

    r = requests.request(method, url, auth=auth, headers=HEADERS, json=payload)
    logger.debug(f"STATUS {r.status_code}")

    if r.text:
        data = r.json()
        logger.debug(json.dumps(data, indent=2))
        r.raise_for_status()
        return data

    return {}

# ---------------- OPS MANAGER LOGIC ----------------

def find_cluster(base_url, group_id, auth, cluster_name):
    url = f"{base_url}/api/public/v1.0/backup/third_party/group/{group_id}/clusters"
    data = api("GET", url, auth)

    for c in data.get("clusters", []):
        if c["clusterName"] == cluster_name:
            logger.info(f"Cluster resolved: {cluster_name}")
            return c["clusterId"]

    raise RuntimeError(f"Cluster '{cluster_name}' not found")

def discover_snapshot_nodes(base_api, auth):
    """
    Returns ONE snapshot node per replicaSet.
    Works for:
      - single node
      - replica set
      - sharded cluster (including configRS)
    """
    data = api("GET", base_api, auth)
    selected = []

    for rs in data.get("replicaSets", []):
        rs_id = rs.get("id")
        nodes = rs.get("nodes", [])
        logger.info(f"Selecting snapshot node for replicaSet: {rs_id}")

        best_node = None
        best_time = None

        # Prefer SECONDARY with latest opTime
        for n in nodes:
            if n.get("snapshotable") and n["memberState"] == "SECONDARY" and n.get("opTime"):
                if best_time is None or n["opTime"] > best_time:
                    best_time = n["opTime"]
                    best_node = n

        # Fallback to PRIMARY
        if not best_node:
            for n in nodes:
                if n.get("snapshotable") and n["memberState"] == "PRIMARY":
                    logger.warning(f"No SECONDARY found in {rs_id}, using PRIMARY")
                    best_node = n
                    break

        if not best_node:
            raise RuntimeError(f"No snapshotable node found for replicaSet {rs_id}")

        logger.info(f"Selected {best_node['id']} for {rs_id}")
        selected.append(best_node["id"])


        ###
        ### for debug purposes
        # selected = []
        # selected = ['mdb7:37017', 'mdb8:37017', 'mdb9:37017']
        ####
        ####

    return selected

def create_snapshot(base_api, auth, node_ids):
    payload = {"nodeIds": node_ids}
    data = api("POST", f"{base_api}/snapshot", auth, payload)
    return data["snapshotId"]

def start_snapshot(base_api, auth, snapshot_id):
    api("POST", f"{base_api}/snapshot/{snapshot_id}/start", auth)

def poll_snapshot(base_api, auth, snapshot_id, target, poll):
    while True:
        data = api("GET", f"{base_api}/snapshot/{snapshot_id}", auth)
        state = data.get("state")
        logger.info(f"Snapshot state: {state}")
        if state == target:
            return data

        ### a snapshot that has failed or been cancelled never reaches the
        ### target state, so stop rather than polling a dead snapshot forever
        if state in TERMINAL_SNAPSHOT_STATES:
            raise RuntimeError(
                f"Ops Manager snapshot {snapshot_id} entered state {state} "
                f"while waiting for {target}"
            )

        time.sleep(poll)

def wait_for_filelists(base_api, auth, snapshot_id, node_ids, poll):
    for node_id in node_ids:
        url = f"{base_api}/snapshot/{snapshot_id}/{node_id}/fileList"
        logger.info(f"Waiting for fileList on {node_id}")

        while True:
            data = api("GET", url, auth)
            if data.get("fileList"):
                logger.info(f"fileList ready on {node_id}")
                break
            time.sleep(poll)

def finish_snapshot(base_api, auth, snapshot_id):
    api("POST", f"{base_api}/snapshot/{snapshot_id}/finish", auth)

def _group_host_metadata(host_metadata):
    grouped = {}
    for entry in host_metadata:
        rs = entry["rs_id"]
        grouped.setdefault(rs, []).append({
            "hostname":    entry["node_id"],
            "mount_point": entry["mount_point"],
            "fs_uuid":     entry["uuid"],
        })
    return grouped

def save_metadata(run_dir, cluster_name, snapshot_id, final_data, ibm_snap, cluster_map, host_metadata):
    path = os.path.join(run_dir, "snapshot_metadata.json")
    with open(path, "w") as f:
        json.dump(
            {
                "cluster_name": cluster_name,
                "snapshot_id": snapshot_id,
                "ibm_flashsystem_snapshot": ibm_snap,
                "infrastructure_map": cluster_map,
                "opsmanager_metadata": final_data,
                "host_metadata": _group_host_metadata(host_metadata),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            f,
            indent=2,
        )
    logger.info(f"Metadata saved: {path}")

def _node_has_replicated_dependent(node_data):
    """A node is DR-protected if its volume group has at least one
    dependent (thin-clone) volume group whose replication policy resolves
    to a target FlashSystem."""
    if not node_data or not node_data.get("volume_group"):
        return False
    return any(clone.get("target") for clone in node_data.get("replicated_clones", []))

def flashsystem_token(system, tokens):
    """Return an auth token for `system`, authenticating on first use and
    reusing it for later calls against the same FlashSystem. Credentials come
    from flash_credentials.py."""
    if system not in tokens:
        entry = next((f for f in flash_credentials.FlashSystems if f["system"] == system), None)
        if entry is None:
            raise RuntimeError(f"No credentials for FlashSystem '{system}' in flash_credentials.py")

        logger.info(f"Authenticating to FlashSystem {system}")
        tokens[system] = flashsystem_utilities.getFlashSystemToken(
            entry["system"], entry["user"], entry["password"]
        )

    return tokens[system]

def flashsystem_for_location(location):
    """Map a replication policy target location (the short array name a policy
    reports, e.g. 'FS9500-3') to the matching FlashSystem address configured in
    flash_credentials.py. Returns None when no configured system matches."""
    if not location:
        return None

    for entry in flash_credentials.FlashSystems:
        if entry["system"].split(".")[0].lower() == location.lower():
            return entry["system"]

    return None

def _nodes_by_id(node_map):
    """Flatten an infrastructure map into {node_id: node_data}."""
    return {
        node["id"]: node
        for shard in node_map.get("shards", [])
        for node in shard["nodes"]
    }

def backup_node_records(node_map, node_ids):
    """Return the infrastructure map entries for the snapshot nodes, in
    `node_ids` order. Each entry carries the shard, FlashSystem and volume
    group used to name and take the FlashSystem snapshot."""
    nodes_by_id = _nodes_by_id(node_map)
    return [nodes_by_id[node_id] for node_id in node_ids if node_id in nodes_by_id]

def review_infrastructure(base_url, group_id, auth, cluster_name, node_ids=None):
    """Run the infrastructure map for `cluster_name` and report, shard by
    shard, whether it is backed by a volume group that has a replicated
    dependent volume group.

    `node_ids`, when given, are the nodes this backup would snapshot, so a
    shard whose backup node is unprotected is called out even when a different
    node in that shard is protected.

    Returns True when nothing was flagged."""
    cluster_map = infrastructure_map.make_infrastructure_map(base_url, group_id, auth, cluster_name)
    selected = set(node_ids or [])

    unprotected_shards = []
    unprotected_backup_nodes = []

    logger.info(f"Reviewing replication coverage for cluster '{cluster_name}'")

    for shard in cluster_map.get("shards", []):
        rs_id = shard["rs_id"]
        protected = [node for node in shard["nodes"] if _node_has_replicated_dependent(node)]

        if not protected:
            unprotected_shards.append(rs_id)
            logger.warning(
                f"Shard {rs_id}: no node is backed by a volume group with a replicated dependent"
            )
            continue

        for node in protected:
            clones = ", ".join(
                f"{clone['name']} -> {clone['target']}"
                for clone in node["replicated_clones"] if clone["target"]
            )
            logger.info(
                f"Shard {rs_id}: {node['id']} on {node['flashsystem']} / "
                f"{node['volume_group']} replicates {clones}"
            )

        for node in shard["nodes"]:
            if node["id"] in selected and not _node_has_replicated_dependent(node):
                unprotected_backup_nodes.append(node["id"])
                logger.warning(
                    f"Shard {rs_id}: backup node {node['id']} has no replicated dependent, "
                    f"though another node in this shard does"
                )

    if unprotected_shards:
        logger.warning(
            f"{len(unprotected_shards)} shard(s) with no replicated dependency: "
            f"{', '.join(unprotected_shards)}"
        )
    if unprotected_backup_nodes:
        logger.warning(
            f"{len(unprotected_backup_nodes)} backup node(s) with no replicated dependency: "
            f"{', '.join(unprotected_backup_nodes)}"
        )
    if not unprotected_shards and not unprotected_backup_nodes:
        logger.info("Every shard is backed by a volume group with a replicated dependent")

    return not unprotected_shards and not unprotected_backup_nodes

def verify_replicated_dependents(node_map, node_ids):
    """Warn if any snapshot node in `node_ids` is not backed by a volume
    group with a replicated dependent, and prompt the user to fix it or
    continue anyway. Aborts (SystemExit) if the user declines to continue."""
    nodes_by_id = _nodes_by_id(node_map)

    unprotected = [
        node_id for node_id in node_ids
        if not _node_has_replicated_dependent(nodes_by_id.get(node_id))
    ]

    if not unprotected:
        logger.info("All snapshot nodes have a replicated dependent volume group")
        return

    logger.warning(
        "The following snapshot node(s) are not backed by a volume group with a "
        f"replicated dependent (no DR target configured): {', '.join(unprotected)}"
    )
    answer = input(
        "Fix replication for these node(s) and re-run, or continue without DR "
        "protection? [fix/continue]: "
    ).strip().lower()

    if answer not in ("continue", "c"):
        raise SystemExit(
            "Aborting so replication can be fixed for: " + ", ".join(unprotected)
        )

    logger.warning(f"Continuing backup without DR replication for: {', '.join(unprotected)}")

# ---------------- MAIN ----------------

def main():
    global logger

    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--group-id", default=None)
    parser.add_argument("--public-key", default=None)
    parser.add_argument("--private-key", default=None)
    parser.add_argument("--cluster-name", required=True)
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument(
        "--nodes",
        default=None,
        metavar="host:port,host:port,...",
        help="Comma-separated list of nodes (name:port) to use as snapshot targets, "
             "overriding automatic node discovery.",
    )
    parser.add_argument(
        "--save-node-data",
        default=None,
        metavar="all|node1,node2,...",
        help="Nodes to include in host_metadata. 'all' saves every cluster node; "
             "a comma-separated list saves only those node IDs; "
             "omit to save only the backup nodes (default).",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="Run the infrastructure map and flag any shard not backed by a volume "
             "group with a replicated dependent, then exit without taking a backup. "
             "Exits non-zero when something is flagged.",
    )
    args = parser.parse_args()
    resolve_credentials(args)

    auth = HTTPDigestAuth(args.public_key, args.private_key)

    run_dir = create_run_dir(args.cluster_name)
    temp_log = os.path.join(run_dir, "snapshot_temp.log")
    logger = setup_logging(temp_log)

    logger.info("Starting Ops Manager snapshot workflow")

    cluster_id = find_cluster(args.base_url, args.group_id, auth, args.cluster_name)
    base_api = f"{args.base_url}/api/public/v1.0/backup/third_party/group/{args.group_id}/clusters/{cluster_id}"

    if args.nodes:
        node_ids = [n.strip() for n in args.nodes.split(",")]
        logger.info(f"Using explicitly provided snapshot nodes: {node_ids}")
    else:
        node_ids = discover_snapshot_nodes(base_api, auth)
    logger.info(f"Snapshot nodes: {node_ids}")

    if args.review:
        logger.info("=== INFRASTRUCTURE REVIEW (no backup will be taken) ===")
        if not review_infrastructure(args.base_url, args.group_id, auth, args.cluster_name, node_ids):
            raise SystemExit(1)
        return

    logger.info("Building infrastructure map for full cluster")
    cluster_map = infrastructure_map.make_infrastructure_map(
        args.base_url, args.group_id, auth, args.cluster_name
    )

    logger.info("Building infrastructure map for snapshot nodes")
    node_map = infrastructure_map.make_infrastructure_map(
        args.base_url, args.group_id, auth, args.cluster_name, nodes=node_ids
    )

    verify_replicated_dependents(node_map, node_ids)

    snapshot_id = create_snapshot(base_api, auth, node_ids)

    final_log = rename_log_file(temp_log, run_dir, snapshot_id)
    logger = setup_logging(final_log)
    logger.info(f"Snapshot ID: {snapshot_id}")

    start_snapshot(base_api, auth, snapshot_id)
    poll_snapshot(base_api, auth, snapshot_id, "READY", args.poll_interval)

    wait_for_filelists(base_api, auth, snapshot_id, node_ids, args.poll_interval)

    logger.info("Flushing filesystem buffers on snapshot nodes")
    for node_id in node_ids:
        host = node_id.split(":")[0]
        logger.info(f"Running filesystem sync on {host}")
        result = subprocess.run(
            ["ssh", host, "sync"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(f"sync failed on {host}: {result.stderr.strip()}")

    ###
    ###
    ### added sleep to confirm filelist is captured
    ###
    ###
    # time.sleep(180)

    logger.info("=== TAKING IBM FLASHSYSTEM SNAPSHOT NOW ===")
    backup_nodes = backup_node_records(node_map, node_ids)
    snapshot_results = []
    snapshot_names = {}
    fs_tokens = {}

    for node in backup_nodes:
        if not node["flashsystem"] or not node["volume_group"]:
            logger.warning(
                f"Skipping FlashSystem snapshot for {node['id']} "
                f"(shard {node['shard']}): no volume group resolved"
            )
            continue

        logger.info(
            f"Snapshotting {node['id']} (shard {node['shard']}): "
            f"{node['flashsystem']} / {node['volume_group']} "
            f"as {node['shard']}_{snapshot_id}"
        )
        result = flashsystem_backup.takeFlashSystemSnapshot(
            node["flashsystem"],
            flashsystem_token(node["flashsystem"], fs_tokens),
            node["volume_group"],
            snapshot_id,
            node["shard"],
        )
        snapshot_results.append(result)
        snapshot_names[node["id"]] = result["snapshot_name"]

    flashsystem_metadata = {
        "snapshot_id": snapshot_id,
        "results": snapshot_results,
    }
    logger.info("FlashSystem snapshot completed successfully")

    logger.info("Opsman: Marking snapshot as finished")
    finish_snapshot(base_api, auth, snapshot_id)

    logger.info("Replicating Snapshots to remote facilities")
    replication_results = []
    refreshed_clones = []

    for node in backup_nodes:
        snapshot_name = snapshot_names.get(node["id"])
        if not snapshot_name:
            continue

        dependents = node["replicated_clones"]
        if not dependents:
            logger.warning(
                f"No replicated dependent volume groups for {node['id']} "
                f"(shard {node['shard']}): nothing to refresh"
            )
            continue

        for clone in dependents:
            logger.info(
                f"Refreshing {clone['name']} from {node['volume_group']} "
                f"snapshot {snapshot_name} (policy {clone['policy']}, target {clone['target']})"
            )
            if not clone["target"]:
                logger.warning(
                    f"Replication policy '{clone['policy']}' on {clone['name']} "
                    f"has no resolved target location; refreshing anyway"
                )

        try:
            refreshed = flashsystem_utilities.replicateFlashSystemSnapshot(
                node["flashsystem"],
                flashsystem_token(node["flashsystem"], fs_tokens),
                node["volume_group"],
                snapshot_name,
                dependents,
            )
        except RuntimeError as e:
            logger.error(
                f"Replication refresh failed for {node['id']} (shard {node['shard']}): {e}"
            )
            continue

        replication_results.extend(refreshed)

        ### only clones that actually refreshed are worth snapshotting remotely;
        ### snapshotting a clone that still holds an older point in time would
        ### create a remote restore point that looks current but is not
        for entry in refreshed:
            refreshed_clones.append({
                "shard": node["shard"],
                "volume_group": entry["volume_group"],
                "target": entry["target"],
            })

    flashsystem_metadata["replication"] = replication_results


    final = poll_snapshot(base_api, auth, snapshot_id, "FINISHED", args.poll_interval)

    logger.info("Collecting host mount metadata")
    all_nodes = get_cluster_nodes(args.base_url, args.group_id, cluster_id, auth)

    if args.save_node_data is None:
        probe_nodes = [n for n in all_nodes if n["id"] in node_ids]
    elif args.save_node_data.lower() == "all":
        probe_nodes = all_nodes
    else:
        requested = set(args.save_node_data.split(","))
        probe_nodes = [n for n in all_nodes if n["id"] in requested]

    host_metadata = detect_cluster_mount_points(probe_nodes)

    save_metadata(
        run_dir,
        args.cluster_name,
        snapshot_id,
        final,
        flashsystem_metadata,
        cluster_map,
        host_metadata,
    )

    logger.info("Snapshot workflow completed successfully")
    logger.info("Sleeping for 60 seconds")
    time.sleep(60)

    logger.info("=== TAKING REMOTE IBM FLASHSYSTEM SNAPSHOT NOW ===")
    remote_results = []

    if not refreshed_clones:
        logger.warning("No volume group was refreshed, so there is no remote snapshot to take")

    for clone in refreshed_clones:
        remote_system = flashsystem_for_location(clone["target"])
        if not remote_system:
            logger.warning(
                f"No FlashSystem configured for replication target '{clone['target']}'; "
                f"skipping remote snapshot of {clone['volume_group']}"
            )
            continue

        logger.info(
            f"Remote snapshot on {remote_system}: {clone['volume_group']} "
            f"as {clone['shard']}_{snapshot_id}"
        )
        remote_results.append(
            flashsystem_backup.takeFlashSystemSnapshot(
                remote_system,
                flashsystem_token(remote_system, fs_tokens),
                clone["volume_group"],
                snapshot_id,
                clone["shard"],
            )
        )

    ### re-save so the metadata records the remote restore points too
    flashsystem_metadata["remote"] = remote_results
    save_metadata(
        run_dir,
        args.cluster_name,
        snapshot_id,
        final,
        flashsystem_metadata,
        cluster_map,
        host_metadata,
    )

    logger.info("=== Backup Complete ===")
   

if __name__ == "__main__":
    main()
