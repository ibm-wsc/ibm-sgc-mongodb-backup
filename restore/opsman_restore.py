#!/usr/bin/env python3

# ####################################################################################
#
# Purpose : Implements an automated MongoDB restore workflow using MongoDB Ops Manager
#           third-party restore APIs in conjunction with IBM FlashSystem Snapshot /
#           Safeguarded Copy.
#
#           The script performs the following:
#             - Discovers MongoDB cluster by name
#             - Enumerates available restore points from backup metadata
#             - Validates restore points against existing IBM FlashSystem snapshots
#               (automatically ignores expired or deleted storage snapshots)
#             - Allows operators to list, select, or automatically choose the
#               latest valid restore point
#             - Initiates and manages Ops Manager third-party restore workflows
#             - Pauses for manual IBM FlashSystem volume clone and mount operations
#             - Coordinates file copy completion and monitors restore progress
#
# Author  : Jai Waghela
#
# Date
# Written : Fri Jan 09 2026
#
# Version : 0.1 : Initial version
#           0.2 : Added restore-point discovery from backup metadata
#           0.3 : Integrated IBM FlashSystem snapshot validation (retention-aware)
#           0.4 : Added support for listing restore points and dry-run mode
#           0.5 : Aligned FlashSystem REST behavior with CLI semantics
#                 (client-side snapshot matching, graceful handling of expired snapshots)
#           0.6 : Enhanced operator prompts with explicit mount paths (dbPath)
#           0.7 : leverages subprocess to unmount the list of hosts and calls upon the FlashSystem volumes to refreshfromsnapshot
#           1.0 : Supports replica and sharded recovery
#           1.1 : Automates the unmounts of the target instances and verifies filesystem UUID mounts
#           1.2 : Ops Manager credentials now come from the OPSMAN_* environment
#                 variables (source opsman.cred); CLI flags still take precedence
#           2.0 : Restores to a new target cluster. The target's own
#                 infrastructure map supplies each node's storage system, volume
#                 group and host object, so a restore no longer requires a
#                 cluster pre-built on a thin clone: a thin clone volume group is
#                 created per node from that shard's snapshot, its volume renamed,
#                 mapped to the resolved host object, attached to the guest's
#                 domain, given a fresh filesystem UUID and mounted.
#                 Shard names are matched against the backup, with --shardmap to
#                 map them explicitly when the target names differ, and a
#                 mismatch is refused rather than guessed.
#                 Adds --validate (snapshot availability on the target's storage,
#                 host objects, hypervisor domains), --restore-id and --nodes to
#                 resume specific nodes, and --opsman-action for Ops Manager
#                 status, files-copied, start and restore listing.
#                 Per-snapshot validation is gone from selection; a restore_metadata.json
#                 and restore log are written beside the backup's own metadata,
#                 recording the shard map, each Ops Manager step, and each node's
#                 volume UID and new filesystem UUID.
#
# Usage   : opsman_restore.py [-h]
#                             --target-cluster CLUSTER_NAME
#                             [--source-cluster CLUSTER_NAME]
#                             [--snapshot-id SNAPSHOT_ID | --latest]
#                             [--list-snapshots]
#                             [--validate]
#                             [--shardmap source:target,...]
#                             [--dry-run]
#                             [--base-url OPSMANAGER_URL]
#                             [--group-id GROUP_ID]
#                             [--public-key PUBLIC_KEY]
#                             [--private-key PRIVATE_KEY]
#                             [--poll-interval SECONDS]
#
#           --source-cluster is only needed with --latest or --list-snapshots;
#           --snapshot-id identifies a backup run on its own.
#
#           --validate maps the target cluster, checks its shard names against
#           the backup, and confirms each snapshot exists on the storage the
#           target nodes are attached to, then exits without restoring.
#
# ####################################################################################

# ####################################################################################
#
#                           **** DISCLAIMER ****
#
# This script is provided as a reference implementation to demonstrate a MongoDB
# restore workflow using Ops Manager third-party APIs and IBM FlashSystem Snapshot /
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


import argparse
import json
import os
import sys
import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
import requests
from requests.auth import HTTPDigestAuth
import warnings
from requests.packages.urllib3.exceptions import InsecureRequestWarning

### repo root on the path before any repo-local import below
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import flash_credentials as fs
from hosts.mount_hosts import unmount_hosts, mount_single_host, verify_single_host_mount
from hosts import mongo_query
from hosts import host_utilities
from hosts import opsman_infrastructure_map as infrastructure_map
from ibmflashsystem import flashsystem_utilities


warnings.simplefilter("ignore", InsecureRequestWarning)

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

#RUNS_DIR = "../backup/runs"
RUNS_DIR = "runs"
HEADERS = {"Content-Type": "application/json"}
logger = None

# ---------------- SIMPLE HELPERS ----------------

def safe_name(name):
    return name.replace(" ", "_").replace("/", "_").replace(":", "_")

def utc_ts():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

# ---------------- LOGGING ----------------

def setup_logging(log_file):
    logger = logging.getLogger("opsmgr_restore")
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


def setup_console_logging():
    """Console-only logger for the Ops Manager actions. They are not tied to a
    backup run, so there is no run directory to write a restore log into."""
    logger = logging.getLogger("opsmgr_restore")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(ch)

    return logger


def restore_log_path(backup_run_dir, snapshot_id):
    """Log path for a restore, in the backup's own run directory beside its
    snapshot_metadata.json. The timestamp keeps repeat restores of the same
    backup from overwriting each other."""
    return os.path.join(backup_run_dir, f"restore_{snapshot_id}_{utc_ts()}.log")


# ---------------- RESTORE METADATA ----------------

def new_restore_metadata(run, target_cluster, shard_mapping):
    """Start the restore record that is written beside the backup's own
    snapshot_metadata.json."""
    return {
        "snapshot_id": run["snapshot_id"],
        "source_cluster": run.get("source_cluster"),
        "target_cluster": target_cluster,
        "backup_run_dir": run["run_dir"],
        "shard_map": dict(shard_mapping),
        "restore_id": None,
        "status": "in_progress",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "opsmanager": [],
        "nodes": [],
    }


def load_or_new_restore_metadata(backup_run_dir, run, target_cluster, shard_mapping, restore_id):
    """Return the restore record to work with.

    Resuming a restore whose id matches the record already on disk continues
    that record, so the per-node step history survives and resume can tell what
    each node already finished. Anything else starts a fresh record.
    """
    path = os.path.join(backup_run_dir, "restore_metadata.json")

    if restore_id and os.path.isfile(path):
        with open(path) as f:
            existing = json.load(f)

        if existing.get("restore_id") == restore_id:
            existing["status"] = "in_progress"
            existing["resumed_at"] = datetime.now(timezone.utc).isoformat()
            existing["shard_map"] = dict(shard_mapping)
            return existing

        logger.warning(
            f"Existing restore record is for restore {existing.get('restore_id')}, "
            f"not {restore_id}; starting a new record"
        )

    return new_restore_metadata(run, target_cluster, shard_mapping)


def node_record_for(metadata, node_id, target_shard, source_shard, snapshot_name):
    """Return this node's record, reusing the one from a previous attempt so
    completed steps and the values they produced are not lost."""
    for record in metadata["nodes"]:
        if record["id"] == node_id:
            record["target_shard"] = target_shard
            record["source_shard"] = source_shard
            record["snapshot_name"] = snapshot_name
            record["status"] = "in_progress"
            record.pop("error", None)
            return record

    record = {
        "id": node_id,
        "target_shard": target_shard,
        "source_shard": source_shard,
        "snapshot_name": snapshot_name,
        "status": "in_progress",
        "steps": [],
    }
    metadata["nodes"].append(record)
    return record


def completed_steps(node_record):
    """Step names this node already finished, from the recorded history."""
    return {s["step"] for s in node_record.get("steps", []) if s.get("status") == "completed"}


def save_restore_metadata(backup_run_dir, metadata):
    """Write the restore record. Called after every recorded step so a run that
    stops partway still leaves a complete account of what was done."""
    path = os.path.join(backup_run_dir, "restore_metadata.json")
    with open(path, "w") as f:
        json.dump(metadata, f, indent=2)
    return path


def record_opsman_step(backup_run_dir, metadata, step, status="completed", **detail):
    """Acknowledge a step completed against Ops Manager."""
    entry = {"step": step, "status": status, "at": datetime.now(timezone.utc).isoformat()}
    if detail:
        entry.update(detail)
    metadata["opsmanager"].append(entry)
    save_restore_metadata(backup_run_dir, metadata)


def record_node_step(backup_run_dir, metadata, node_record, step, status="completed", **fields):
    """Acknowledge a step completed against one target node, recording any
    values it produced (the new volume UID, the new filesystem UUID)."""
    node_record.setdefault("steps", []).append(
        {"step": step, "status": status, "at": datetime.now(timezone.utc).isoformat()}
    )
    node_record.update(fields)
    save_restore_metadata(backup_run_dir, metadata)


# ---------------- CALL LOGGING ----------------

def logged(fn, *args, **kwargs):
    """Call `fn`, logging the call and its result into the restore log so the
    log carries the full trace of what the restore did.

    String arguments longer than 60 characters are redacted, which keeps
    FlashSystem auth tokens out of the log.
    """
    def show(value):
        if isinstance(value, str) and len(value) > 60:
            return "<redacted>"
        return repr(value)

    shown = ", ".join(
        [show(a) for a in args] + [f"{k}={show(v)}" for k, v in kwargs.items()]
    )
    logger.info(f"call  {fn.__name__}({shown})")

    result = fn(*args, **kwargs)

    logger.info(f"ret   {fn.__name__} -> {show(result)}")
    return result

# ---------------- API WRAPPER ----------------

def api(method, url, auth, payload=None):
    logger.debug(f"API {method} {url}")
    if payload:
        logger.debug(json.dumps(payload, indent=2))

    r = requests.request(
        method,
        url,
        auth=auth,
        headers=HEADERS,
        json=payload,
        timeout=60,
    )

    logger.debug(f"STATUS {r.status_code}")
    if r.text:
        data = r.json()
        logger.debug(json.dumps(data, indent=2))
        r.raise_for_status()
        return data

    return {}

# ---------------- Backup metadata discovery ----------------

def discover_backup_runs(cluster_name=None):
    """Load every backup run under runs/, newest first.

    `cluster_name` narrows the scan to one source cluster's runs, which is how
    --latest and --list-snapshots stay scoped. Selecting by snapshot id needs
    no cluster name, since the id identifies the run on its own and the run's
    own metadata records which cluster produced it.
    """
    cname = safe_name(cluster_name) if cluster_name else None
    runs = []

    if not os.path.isdir(RUNS_DIR):
        return runs

    for d in os.listdir(RUNS_DIR):
        if cname and not d.startswith(cname + "_"):
            continue

        meta_file = os.path.join(RUNS_DIR, d, "snapshot_metadata.json")
        if not os.path.exists(meta_file):
            continue

        with open(meta_file) as f:
            meta = json.load(f)

        runs.append({
            "run_dir": os.path.join(RUNS_DIR, d),
            "snapshot_id": meta["snapshot_id"],
            "source_cluster": meta.get("cluster_name"),
            "timestamp": meta.get("created_at"),
            "metadata": meta,
        })

    runs.sort(key=lambda x: x["timestamp"], reverse=True)
    return runs

# ---------------- Shard mapping ----------------

def flashsystem_token(system, tokens):
    """Return an auth token for `system`, authenticating on first use and
    reusing it afterwards. Credentials come from flash_credentials.py."""
    if system not in tokens:
        entry = next((f for f in fs.FlashSystems if f["system"] == system), None)
        if entry is None:
            raise RuntimeError(f"No credentials for FlashSystem '{system}' in flash_credentials.py")

        logger.info(f"Authenticating to FlashSystem {system}")
        tokens[system] = flashsystem_utilities.getFlashSystemToken(
            entry["system"], entry["user"], entry["password"]
        )

    return tokens[system]


def snapshots_by_shard(run_metadata):
    """Return {source shard: snapshot name} from the backup's recorded
    FlashSystem snapshots."""
    return {
        r["shard"]: r["snapshot_name"]
        for r in run_metadata["ibm_flashsystem_snapshot"]["results"]
        if r.get("shard")
    }


def target_nodes_by_shard(target_map):
    """Return {shard: [node, ...]} from a target cluster infrastructure map."""
    return {
        shard["rs_id"]: shard["nodes"]
        for shard in target_map.get("shards", [])
    }


def parse_shard_map(shardmap):
    """Parse 'source:target,source:target' into {source: target}."""
    mapping = {}

    for pair in shardmap.split(","):
        pair = pair.strip()
        if not pair:
            continue

        if pair.count(":") != 1:
            raise SystemExit(
                f"Invalid --shardmap entry '{pair}'. Expected source:target pairs, "
                f"e.g. configRS:tgtconfigRS,sgcShard_0:tgtShard_0"
            )

        source, target = (p.strip() for p in pair.split(":"))
        if not source or not target:
            raise SystemExit(f"Invalid --shardmap entry '{pair}': both names are required")

        if source in mapping:
            raise SystemExit(f"--shardmap names source shard '{source}' more than once")

        mapping[source] = target

    return mapping


def resolve_shard_mapping(run_metadata, target_map, shardmap=None):
    """Work out which target shard receives each source shard's snapshot.

    Without --shardmap the shard names must match the backup's exactly.
    Raises SystemExit describing the mismatch when they do not.
    """
    source_shards = set(snapshots_by_shard(run_metadata))
    target_shards = set(target_nodes_by_shard(target_map))

    if not source_shards:
        raise SystemExit("Backup metadata records no shard names; it predates shard-aware backups")

    if shardmap:
        mapping = parse_shard_map(shardmap)

        unknown_source = sorted(set(mapping) - source_shards)
        if unknown_source:
            raise SystemExit(
                "MISMATCHED SHARD NAMES: --shardmap names source shard(s) not in this backup: "
                f"{', '.join(unknown_source)}. Backup has: {', '.join(sorted(source_shards))}"
            )

        unknown_target = sorted(set(mapping.values()) - target_shards)
        if unknown_target:
            raise SystemExit(
                "MISMATCHED SHARD NAMES: --shardmap names target shard(s) not in the target cluster: "
                f"{', '.join(unknown_target)}. Target has: {', '.join(sorted(target_shards))}"
            )

        uncovered = sorted(source_shards - set(mapping))
        if uncovered:
            raise SystemExit(
                "MISMATCHED SHARD NAMES: --shardmap does not cover source shard(s): "
                f"{', '.join(uncovered)}"
            )

        targets = list(mapping.values())
        if len(targets) != len(set(targets)):
            raise SystemExit("MISMATCHED SHARD NAMES: --shardmap maps more than one source shard to the same target")

        logger.info("Shard mapping supplied by --shardmap:")
        for source, target in mapping.items():
            logger.info(f"  {source} -> {target}")

        return mapping

    missing = sorted(source_shards - target_shards)
    if missing:
        raise SystemExit(
            "MISMATCHED SHARD NAMES: the target cluster has no shard(s) named "
            f"{', '.join(missing)}.\n"
            f"  Backup shards : {', '.join(sorted(source_shards))}\n"
            f"  Target shards : {', '.join(sorted(target_shards))}\n"
            "Supply --shardmap source:target,... to map them explicitly."
        )

    mapping = {shard: shard for shard in sorted(source_shards)}
    logger.info(f"Shard names match the target cluster: {', '.join(sorted(source_shards))}")

    return mapping


def restore_node_storage(node, context, node_record):
    """Clone, map, attach and mount one target node's restore volume.

    Every step is recorded in `node_record` as it completes, so a failure
    leaves behind exactly how far this node got and what was created.
    Raises RuntimeError on any step that cannot be completed.
    """
    backup_run_dir = context["backup_run_dir"]
    metadata = context["metadata"]
    infra = context["infra"]
    tokens = context["tokens"]

    node_id = node["id"]
    host = node_id.split(":")[0]
    guest = infra.guests.get(host)
    if guest is None:
        raise RuntimeError(f"{host} is not present in infrastructure.map")

    ### a resumed node picks up from its recorded history rather than redoing
    ### work that already landed on the array or the hypervisor
    done = completed_steps(node_record)
    if done:
        logger.info(f"{host}: resuming, already completed {sorted(done)}")

    system = node.get("flashsystem") or guest.get("storage")
    if not system:
        raise RuntimeError(f"{host}: no FlashSystem resolved for this node")

    hypervisor = guest.get("hypervisor", "none")
    virtualised = bool(hypervisor) and hypervisor.lower() != "none"
    scan_host = hypervisor if virtualised else host

    token = flashsystem_token(system, tokens)

    ### the snapshot's numeric id is per array, so it has to be looked up on
    ### the array this node is attached to
    found = logged(flashsystem_utilities.getSnapshotVolumeGroup, system, token, context["snapshot_name"])
    if not found:
        raise RuntimeError(f"{host}: snapshot '{context['snapshot_name']}' not found on {system}")
    source_volume_group, snapshot_id = found

    volume_group = flashsystem_utilities.restoreVolumeGroupName(
        node_id, context["target_cluster"], context["mdb_snapshot_id"]
    )
    volume_name = "restore_" + volume_group

    if "create_thinclone_volume_group" in done:
        volume_group = node_record["volume_group"]
        logger.info(f"{host}: reusing existing thin clone {volume_group}")
    else:
        logged(flashsystem_utilities.createThinCloneVolumeGroup, system, token, snapshot_id, volume_group)
        record_node_step(
            backup_run_dir, metadata, node_record, "create_thinclone_volume_group",
            flashsystem=system, source_volume_group=source_volume_group,
            volume_group=volume_group,
        )

    if "rename_volumes" in done:
        volume_name = node_record["volume_name"]
    else:
        logged(flashsystem_utilities.renameVolumeGroupVolumes, system, token, volume_group, volume_name)
        record_node_step(backup_run_dir, metadata, node_record, "rename_volumes", volume_name=volume_name)

    if "read_volume_uid" in done:
        volume_uid = node_record["volume_uid"]
    else:
        volume_uid = logged(flashsystem_utilities.getVolumeUUID, system, token, volume_name)
        if not volume_uid:
            raise RuntimeError(f"{host}: could not read the UUID of volume '{volume_name}'")
        record_node_step(backup_run_dir, metadata, node_record, "read_volume_uid", volume_uid=volume_uid)

    host_object = infrastructure_map.mapping_host_object(infra, host)
    if not host_object:
        raise RuntimeError(f"{host}: no host object resolved from infrastructure.map")

    if "map_volume_to_host" in done:
        logger.info(f"{host}: volume already mapped to {host_object}")
    else:
        logged(flashsystem_utilities.mapVolumeToHost, system, token, volume_name, host_object)
        record_node_step(
            backup_run_dir, metadata, node_record, "map_volume_to_host",
            host_object=host_object, hypervisor=hypervisor if virtualised else None,
        )

    if "rescan_and_find_disk" in done:
        disk = node_record["host_disk"]
    else:
        logged(host_utilities.scan_scsi_devices, scan_host)
        alias = logged(
            host_utilities.find_multipath_device_from_scsi_uuid, scan_host, volume_uid,
            retries=6, delay=5,
        )
        if not alias:
            raise RuntimeError(f"{scan_host}: no multipath device appeared for volume UID {volume_uid}")
        disk = f"/dev/mapper/{alias}"
        record_node_step(backup_run_dir, metadata, node_record, "rescan_and_find_disk", host_disk=disk)

    if virtualised and "attach_disk_to_domain" in done:
        guest_disk = node_record["guest_device"]
        logger.info(f"{host}: disk already attached as {guest_disk}")
    elif virtualised:
        ### attach the whole disk, never a partition
        guest_disk = logged(host_utilities.attach_disk_to_domain, hypervisor, host, disk)
        if not guest_disk:
            raise RuntimeError(f"{hypervisor}: could not attach {disk} to domain {host}")
        record_node_step(backup_run_dir, metadata, node_record, "attach_disk_to_domain", guest_device=guest_disk)
    else:
        guest_disk = disk

    if "resolve_partition" in done:
        partition = node_record["partition"]
    else:
        partition = logged(host_utilities.highest_partition, host, guest_disk)
        if not partition:
            raise RuntimeError(f"{host}: no partition found on {guest_disk}")
        record_node_step(backup_run_dir, metadata, node_record, "resolve_partition", partition=partition)

    if "verify_snapshot_filesystem_uuid" in done:
        logger.info(f"{host}: snapshot filesystem UUID already verified")
    else:
        seen_uuid = logged(host_utilities.filesystem_uuid, host, partition)
        if seen_uuid != context["source_fs_uuid"]:
            raise RuntimeError(
                f"{host}: {partition} has filesystem UUID {seen_uuid}, expected "
                f"{context['source_fs_uuid']} from the snapshot"
            )
        record_node_step(
            backup_run_dir, metadata, node_record, "verify_snapshot_filesystem_uuid",
            source_filesystem_uuid=seen_uuid,
        )

    if "change_filesystem_uuid" in done:
        new_uuid = node_record["new_filesystem_uuid"]
        logger.info(f"{host}: filesystem UUID already reassigned to {new_uuid}")
    else:
        new_uuid = logged(host_utilities.change_filesystem_uuid, host, partition)
        if not new_uuid:
            raise RuntimeError(f"{host}: could not assign a new filesystem UUID on {partition}")
        record_node_step(
            backup_run_dir, metadata, node_record, "change_filesystem_uuid",
            new_filesystem_uuid=new_uuid,
        )

    logged(
        mount_single_host, node_id, run_host_metadata(context),
        fs_uuid=new_uuid, mount_point=context["mount_point"],
    )
    logged(verify_single_host_mount, node_id, run_host_metadata(context), mount_point=context["mount_point"])
    record_node_step(
        backup_run_dir, metadata, node_record, "mount_and_verify",
        mount_point=context["mount_point"],
    )

    return node_record


def run_host_metadata(context):
    """The backup's host_metadata, which mount_single_host still accepts even
    though the restore now supplies the UUID and mount point explicitly."""
    return context["host_metadata"]


def validate_target_hosts(target_map, mapping):
    """Check each target node can actually receive a restore volume.

    Two things have to hold per node. The FlashSystem host object the volume
    would be mapped to must exist on the array that node's storage is on, which
    for a virtualised node means its hypervisor's host object rather than its
    own. And where a node runs on a hypervisor, that hypervisor must have a
    domain of the node's name defined, checked over virsh.

    Returns True when every node in the mapped target shards checks out.
    """
    infra = infrastructure_map.load_infrastructure_credentials()
    nodes_by_shard = target_nodes_by_shard(target_map)
    tokens = {}
    ok = True

    for target_shard in mapping.values():
        for node in nodes_by_shard.get(target_shard, []):
            host = node["id"].split(":")[0]
            guest = infra.guests.get(host)

            if guest is None:
                logger.error(f"{target_shard}/{host}: not present in infrastructure.map")
                ok = False
                continue

            ### the volume is mapped on whichever array this node's storage is
            ### on, so the host object has to exist there
            system = node.get("flashsystem") or guest.get("storage")
            host_object = infrastructure_map.mapping_host_object(infra, host)

            if not host_object:
                logger.error(
                    f"{target_shard}/{host}: no host object resolved from infrastructure.map "
                    f"(hypervisor {guest.get('hypervisor')})"
                )
                ok = False
            elif not system:
                logger.error(f"{target_shard}/{host}: no storage system recorded for '{host_object}'")
                ok = False
            else:
                try:
                    exists = flashsystem_utilities.findHost(
                        system, flashsystem_token(system, tokens), host_object
                    )
                except RuntimeError as e:
                    logger.error(f"{target_shard}/{host}: {e}")
                    exists = False
                    ok = False

                if exists:
                    logger.info(
                        f"{target_shard}/{host}: host object '{host_object}' present on {system}"
                    )
                else:
                    logger.error(
                        f"{target_shard}/{host}: host object '{host_object}' not found on {system}"
                    )
                    ok = False

            hypervisor = guest.get("hypervisor", "none")
            if hypervisor and hypervisor.lower() != "none":
                state = host_utilities.find_domain_on_hypervisor(hypervisor, host)
                if state:
                    logger.info(f"{target_shard}/{host}: defined on {hypervisor} ({state})")
                else:
                    logger.error(f"{target_shard}/{host}: no domain '{host}' found on {hypervisor}")
                    ok = False

    return ok


def validate_target_snapshots(run_metadata, target_map, mapping):
    """Confirm each source shard's snapshot exists on the FlashSystem that the
    mapped target shard's nodes are actually attached to.

    Returns True when every mapping checks out.
    """
    snapshots = snapshots_by_shard(run_metadata)
    nodes_by_shard = target_nodes_by_shard(target_map)
    tokens = {}
    ok = True

    for source_shard, target_shard in mapping.items():
        snapshot_name = snapshots[source_shard]
        nodes = nodes_by_shard.get(target_shard, [])

        systems = sorted({n["flashsystem"] for n in nodes if n.get("flashsystem")})
        if not systems:
            logger.error(
                f"{source_shard} -> {target_shard}: no target node resolves to a FlashSystem, "
                f"so '{snapshot_name}' cannot be verified"
            )
            ok = False
            continue

        for system in systems:
            try:
                found = flashsystem_utilities.getSnapshotVolumeGroup(
                    system, flashsystem_token(system, tokens), snapshot_name
                )
            except RuntimeError as e:
                logger.error(f"{source_shard} -> {target_shard}: {e}")
                ok = False
                continue

            if not found:
                logger.error(
                    f"{source_shard} -> {target_shard}: snapshot '{snapshot_name}' "
                    f"not found on {system}"
                )
                ok = False
                continue

            volume_group, _ = found
            logger.info(
                f"{source_shard} -> {target_shard}: '{snapshot_name}' available on "
                f"{system} (volume group {volume_group})"
            )

    return ok

# ---------------- Snapshot selection & listing ----------------

def list_snapshots(cluster_name):
    """List the restore points recorded for one source cluster.

    This reads metadata only. Whether a snapshot is still present on the
    storage the target will restore from is what --validate checks.
    """
    runs = discover_backup_runs(cluster_name)

    if not runs:
        print(f"No restore points found for cluster '{cluster_name}'")
        return

    print(f"\nRestore points recorded for cluster '{cluster_name}':\n")
    for r in runs:
        print(f"SnapshotId : {r['snapshot_id']}")
        print(f"Timestamp  : {r['timestamp']}")
        for fsr in r["metadata"]["ibm_flashsystem_snapshot"]["results"]:
            print(f"Shard       : {fsr.get('shard')}")
            print(f"FlashSystem : {fsr['flashsystem']}")
            print(f"VolumeGroup : {fsr['volume_group']}")
            print(f"Snapshot    : {fsr['snapshot_name']}")
        print("-" * 60)

def select_snapshot(snapshot_id=None, latest=False, cluster_name=None):
    """Pick the backup run to restore from, by snapshot id or the newest run.

    Selection is a metadata read; the snapshots themselves are checked against
    the target's storage by --validate rather than here.
    """
    runs = discover_backup_runs(cluster_name)
    if not runs:
        raise RuntimeError("No backup metadata found")

    if latest:
        return runs[0]

    for r in runs:
        if r["snapshot_id"] == snapshot_id:
            return r

    raise RuntimeError(f"Snapshot {snapshot_id} not found in {RUNS_DIR}")


# ---------------- OPS MANAGER LOGIC ----------------

def find_cluster(base_url, group_id, auth, cluster_name):
    url = f"{base_url}/api/public/v1.0/backup/third_party/group/{group_id}/clusters"
    data = api("GET", url, auth)

    for c in data.get("clusters", []):
        if c["clusterName"] == cluster_name:
            return c["clusterId"]

    raise RuntimeError(f"Cluster '{cluster_name}' not found")

def get_cluster_topology(base_url, group_id, cluster_id, auth):
    url = f"{base_url}/api/public/v1.0/backup/third_party/group/{group_id}/clusters/{cluster_id}"
    return api("GET", url, auth)

def validate_cluster_topology(snapshot_meta, dest_cluster_data):
    source_rs = len(snapshot_meta["opsmanager_metadata"]["snapshotMetadata"]["rsSnapshotsMetadata"])
    dest_rs = len(dest_cluster_data.get("replicaSets", []))

    logger.info(f"Source replica sets : {source_rs}")
    logger.info(f"Destination replica sets : {dest_rs}")

    if dest_rs < source_rs:
        raise RuntimeError(
            "Destination cluster has fewer replica sets than source backup.\n"
            f"Source : {source_rs}, Destination : {dest_rs}"
        )

    logger.info("Cluster topology validation passed")

def get_cluster_nodes(base_url, group_id, cluster_id, auth):
    url = f"{base_url}/api/public/v1.0/backup/third_party/group/{group_id}/clusters/{cluster_id}"
    data = api("GET", url, auth)

    nodes = []
    for rs in data.get("replicaSets", []):
        for n in rs.get("nodes", []):
            nodes.append({"id": n["id"], "dbPath": n["dbPath"]})
    return nodes

def start_restore(base_url, group_id, cluster_id, snapshot_meta, nodes, auth):
    payload = {
        "snapshotsMetadata": [snapshot_meta],
        "nodes": [{"id": n["id"], "restoreRole": "RESTORE"} for n in nodes],
    }

    url = f"{base_url}/api/public/v1.0/backup/third_party/group/{group_id}/clusters/{cluster_id}/restore"
    resp = api("POST", url, auth, payload)
    return resp["restoreId"]

def start_restore_execution(base_url, group_id, restore_id, auth):
    url = f"{base_url}/api/public/v1.0/backup/third_party/group/{group_id}/restore/{restore_id}/start"
    api("POST", url, auth)

def poll_restore(base_url, group_id, restore_id, auth):
    url = f"{base_url}/api/public/v1.0/backup/third_party/group/{group_id}/restore/{restore_id}"
    return api("GET", url, auth)

def list_restores(target_cluster=None):
    """List the restores recorded under runs/.

    The third-party backup API has no endpoint that enumerates restores: a
    restore can be created, fetched, started or marked by id, and that is all.
    So this reads the restore_metadata.json each backup run carries, which is
    where the ids we created are recorded.
    """
    records = []

    if not os.path.isdir(RUNS_DIR):
        return records

    for d in sorted(os.listdir(RUNS_DIR)):
        path = os.path.join(RUNS_DIR, d, "restore_metadata.json")
        if not os.path.exists(path):
            continue

        with open(path) as f:
            meta = json.load(f)

        if target_cluster and meta.get("target_cluster") != target_cluster:
            continue

        nodes = meta.get("nodes", [])
        records.append({
            "restore_id": meta.get("restore_id"),
            "target_cluster": meta.get("target_cluster"),
            "source_cluster": meta.get("source_cluster"),
            "snapshot_id": meta.get("snapshot_id"),
            "status": meta.get("status"),
            "started_at": meta.get("started_at"),
            "nodes_completed": sum(1 for n in nodes if n.get("status") == "completed"),
            "nodes_total": len(nodes),
            "run_dir": os.path.join(RUNS_DIR, d),
        })

    return records


def run_opsman_action(args, auth):
    """Act on Ops Manager directly, then exit.

    Output goes to the console rather than a restore log, because these actions
    are not tied to a backup run and so have no run directory to write into.
    """
    global logger
    logger = setup_console_logging()

    ### 'start' without a restore id means create one, which needs a backup run,
    ### the target map and the shard mapping, so that case is handled in the
    ### main flow rather than here
    if args.opsman_action not in ("list-restores", "start") and not args.restore_id:
        raise SystemExit(f"--restore-id is required with --opsman-action {args.opsman_action}")

    ### listing reads our own records, so it needs no cluster lookup
    if args.opsman_action == "list-restores":
        records = list_restores(args.target_cluster)
        if not records:
            print(f"No recorded restores for cluster '{args.target_cluster}'")
            return

        print(f"\nRecorded restores targeting '{args.target_cluster}':\n")
        for r in records:
            print(f"  restore id : {r['restore_id']}")
            print(f"  from       : {r['source_cluster']} snapshot {r['snapshot_id']}")
            print(f"  status     : {r['status']}  ({r['nodes_completed']} of {r['nodes_total']} nodes completed)")
            print(f"  started    : {r['started_at']}")
            print(f"  record     : {r['run_dir']}")
            print("  " + "-" * 58)
        return

    cluster_id = find_cluster(args.base_url, args.group_id, auth, args.target_cluster)

    if args.opsman_action == "status":
        status = poll_restore(args.base_url, args.group_id, args.restore_id, auth)
        print(f"\nRestore {args.restore_id}")
        print(f"  state: {status.get('state')}\n")
        for n in status.get("nodes", []):
            print(f"  {n.get('id'):<20} {n.get('state')}")
        return

    if args.opsman_action == "start":
        start_restore_execution(args.base_url, args.group_id, args.restore_id, auth)
        print(f"Execution started for restore {args.restore_id}")
        return

    if args.opsman_action == "files-copied":
        if not args.nodes:
            raise SystemExit("--nodes is required with --opsman-action files-copied")

        wanted = [n.strip() for n in args.nodes.split(",")]

        try:
            db_paths = {n["id"]: n["dbPath"] for n in get_cluster_nodes(
                args.base_url, args.group_id, cluster_id, auth
            )}
        except requests.exceptions.HTTPError as e:
            ### during COPY_FILES mongod is stopped on the targets, so Ops
            ### Manager's third-party discovery of the cluster fails. The
            ### restore itself still lists its nodes, and the dbPath comes from
            ### the backup run being restored.
            logger.warning(f"Cluster lookup unavailable ({e}); falling back to the restore's node list")

            status = poll_restore(args.base_url, args.group_id, args.restore_id, auth)
            restore_nodes = [n["id"] for n in status.get("nodes", [])]

            if args.latest or args.snapshot_id:
                run = select_snapshot(
                    snapshot_id=args.snapshot_id,
                    latest=bool(args.latest),
                    cluster_name=args.source_cluster,
                )
                mount_points = {
                    entry["mount_point"]
                    for entries in run["metadata"]["host_metadata"].values()
                    for entry in entries
                }
                if len(mount_points) != 1:
                    raise SystemExit(
                        "Cannot derive a dbPath: the backup records more than one "
                        f"mount point ({', '.join(sorted(mount_points))})"
                    )
                db_path = mount_points.pop() + "/data"
            else:
                raise SystemExit(
                    "Cluster lookup failed and no backup run was given to take the "
                    "dbPath from; add --latest or --snapshot-id (with --source-cluster)"
                )

            logger.info(f"Using dbPath {db_path} for every node")
            db_paths = {node_id: db_path for node_id in restore_nodes}

        for node_id in wanted:
            if node_id not in db_paths:
                raise SystemExit(f"{node_id} is not a node of cluster '{args.target_cluster}'")

            files_copied(
                args.base_url, args.group_id, args.restore_id,
                node_id, db_paths[node_id], auth,
            )
            print(f"Marked {node_id} as filesCopied on restore {args.restore_id}")
        return


def wait_for_node_state(base_url, group_id, restore_id, node_id, target_state, auth, poll,
                        max_polls=60):
    """Wait until Ops Manager reports `node_id` in `target_state`.

    Bounded rather than open ended, so a node that never gets there fails the
    restore instead of stalling it. Returns True once the state is seen.
    """
    for attempt in range(max_polls):
        status = poll_restore(base_url, group_id, restore_id, auth)

        for n in status.get("nodes", []):
            if n["id"] == node_id and n.get("state") == target_state:
                return True

        logger.info(
            f"Waiting for {node_id} to reach {target_state} "
            f"(attempt {attempt + 1} of {max_polls}, restore state {status.get('state')})"
        )
        time.sleep(poll)

    return False


def files_copied(base_url, group_id, restore_id, node_id, db_path, auth):
    url = f"{base_url}/api/public/v1.0/backup/third_party/group/{group_id}/restore/{restore_id}/filesCopied"
    api("POST", url, auth, {"id": node_id, "dbPath": db_path})

# ---------------- MAIN ----------------

def main():
    global logger

    parser = argparse.ArgumentParser(description="Ops Manager Third-Party Restore")
    parser.add_argument(
        "--source-cluster",
        default=None,
        help="Source cluster whose backups to look at. Only needed with --latest "
             "or --list-snapshots; selecting by --snapshot-id does not require it.",
    )
    parser.add_argument("--target-cluster", required=True)
    parser.add_argument("--snapshot-id")
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--list-snapshots", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Build the target cluster's infrastructure map, check its shard names "
             "against the backup, and verify each snapshot is present on the storage "
             "the target nodes are attached to. Exits without restoring.",
    )
    parser.add_argument(
        "--shardmap",
        default=None,
        metavar="source:target,...",
        help="Map backup shard names onto target shard names when they differ, "
             "e.g. configRS:tgtconfigRS,sgcShard_0:tgtShard_0",
    )
    parser.add_argument(
        "--restore-id",
        default=None,
        help="Attach to an existing Ops Manager restore instead of creating one. "
             "In a restore this resumes that restore: the Ops Manager setup steps "
             "are skipped and per-node work picks up where the recorded run left off.",
    )
    parser.add_argument(
        "--nodes",
        default=None,
        metavar="host:port,host:port,...",
        help="Restrict the work to these target nodes, for resuming specific nodes "
             "or aiming an --opsman-action at them. Omit for every node in the "
             "mapped shards.",
    )
    parser.add_argument(
        "--opsman-action",
        default=None,
        choices=["status", "files-copied", "start", "list-restores"],
        help="Act on Ops Manager and exit. 'status' reports a restore and each "
             "node's state, 'files-copied' marks --nodes as copied, both needing "
             "--restore-id. 'start' with --restore-id triggers execution on that "
             "restore; without one it creates and starts a new restore, building "
             "the target infrastructure map and resolving the shard mapping first, "
             "then reports the new id to drive nodes with. 'list-restores' reads "
             "the restores recorded under runs/.",
    )
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--group-id", default=None)
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--public-key", default=None)
    parser.add_argument("--private-key", default=None)
    args = parser.parse_args()
    resolve_credentials(args)

    auth = HTTPDigestAuth(args.public_key, args.private_key)

    ### Most Ops Manager actions stand alone: they need no backup run, so they
    ### run before any run selection and print to the console. The exception is
    ### 'start' with no restore id, which means create a new restore, and that
    ### needs the backup run, the target map and the shard mapping first.
    creating_restore = args.opsman_action == "start" and not args.restore_id

    if args.opsman_action and not creating_restore:
        run_opsman_action(args, auth)
        return

    ### --latest and --list-snapshots work across whichever cluster's runs are
    ### on disk, so they need a source cluster to scope them; --snapshot-id
    ### identifies a single run on its own
    if (args.latest or args.list_snapshots) and not args.source_cluster:
        raise SystemExit("--source-cluster is required with --latest or --list-snapshots")

    if args.list_snapshots:
        list_snapshots(args.source_cluster)
        return

    ### the run has to be selected before logging starts, because the log lives
    ### in that run's own directory beside its snapshot_metadata.json. Nothing
    ### on the selection path logs, so nothing is lost by waiting.
    if args.latest or (args.dry_run and not args.snapshot_id):
        run = select_snapshot(latest=True, cluster_name=args.source_cluster)
    elif args.snapshot_id:
        run = select_snapshot(snapshot_id=args.snapshot_id, cluster_name=args.source_cluster)
    else:
        raise SystemExit("Use --latest or --snapshot-id <ID>")

    final_log = restore_log_path(run["run_dir"], run["snapshot_id"])
    logger = setup_logging(final_log)

    logger.info("Starting Ops Manager restore workflow")
    logger.info(
        f"Restore point {run['snapshot_id']} taken from cluster "
        f"'{run.get('source_cluster')}' at {run['timestamp']}"
    )
    logger.info(f"Restore log: {final_log}")

    if args.dry_run:
        logger.info("DRY RUN — no restore APIs will be called")
        logger.info(json.dumps(run["metadata"], indent=2))
        return

    ### the target's own map is what says which storage each shard sits on, and
    ### the shard mapping has to hold before any restore work begins
    logger.info(f"Building infrastructure map for target cluster '{args.target_cluster}'")
    target_map = infrastructure_map.make_infrastructure_map(
        args.base_url, args.group_id, auth, args.target_cluster
    )

    shard_mapping = resolve_shard_mapping(run["metadata"], target_map, args.shardmap)

    if args.validate:
        logger.info("=== VALIDATION (no restore will be performed) ===")

        logger.info("Checking snapshot availability on the target's storage")
        snapshots_ok = validate_target_snapshots(run["metadata"], target_map, shard_mapping)

        logger.info("Checking target host objects and hypervisor domains")
        hosts_ok = validate_target_hosts(target_map, shard_mapping)

        if not (snapshots_ok and hosts_ok):
            raise SystemExit(1)

        logger.info(
            "Validation passed: snapshots available, host objects present, "
            "hypervisor domains defined"
        )
        return

    backup_run_dir = run["run_dir"]
    metadata = load_or_new_restore_metadata(backup_run_dir, run, args.target_cluster,
                                            shard_mapping, args.restore_id)
    logger.info(f"Restore record: {save_restore_metadata(backup_run_dir, metadata)}")

    snapshot_meta = run["metadata"]["opsmanager_metadata"]["snapshotMetadata"]

    # Resolve target cluster
    dest_cluster_id = logged(find_cluster, args.base_url, args.group_id, auth, args.target_cluster)
    record_opsman_step(backup_run_dir, metadata, "find_cluster", cluster_id=dest_cluster_id)

    nodes = logged(get_cluster_nodes, args.base_url, args.group_id, dest_cluster_id, auth)

    if args.restore_id:
        ### resuming: the restore already exists and is mid-flight, so creating
        ### another would orphan this one and double up on Ops Manager
        restore_id = args.restore_id
        metadata["restore_id"] = restore_id
        record_opsman_step(backup_run_dir, metadata, "resume_restore", restore_id=restore_id)
        logger.info(f"Resuming existing restore {restore_id}")
    else:
        # Fetch destination topology
        dest_topology = logged(get_cluster_topology, args.base_url, args.group_id, dest_cluster_id, auth)

        # Validate topology compatibility
        validate_cluster_topology(run["metadata"], dest_topology)
        record_opsman_step(backup_run_dir, metadata, "validate_cluster_topology")

        restore_id = logged(
            start_restore,
            args.base_url,
            args.group_id,
            dest_cluster_id,
            snapshot_meta,
            nodes,
            auth,
        )

        metadata["restore_id"] = restore_id
        record_opsman_step(backup_run_dir, metadata, "start_restore", restore_id=restore_id)
        logger.info(f"Restore ID: {restore_id}")

        logged(start_restore_execution, args.base_url, args.group_id, restore_id, auth)
        record_opsman_step(backup_run_dir, metadata, "start_restore_execution")

    ### --opsman-action start stops here: the restore exists and is running, and
    ### the per-node work is then driven with --restore-id, optionally --nodes
    if creating_restore:
        logger.info(
            f"Restore {restore_id} created and started. Drive the nodes with "
            f"--restore-id {restore_id}"
        )
        save_restore_metadata(backup_run_dir, metadata)
        print(f"\nRestore id: {restore_id}")
        print(f"Record    : {os.path.join(backup_run_dir, 'restore_metadata.json')}")
        return



    state = ""
    while state != "COPY_FILES":
        status = poll_restore(args.base_url, args.group_id, restore_id, auth)
        state = status.get("state")
        logger.info(f"Restore state: {state}")
        time.sleep(args.poll_interval)



    ## state == "COPY_FILES":

    record_opsman_step(backup_run_dir, metadata, "state_copy_files")

    infra = infrastructure_map.load_infrastructure_credentials()
    host_metadata = run["metadata"]["host_metadata"]
    snapshots = snapshots_by_shard(run["metadata"])
    nodes_by_shard = target_nodes_by_shard(target_map)
    db_paths = {n["id"]: n["dbPath"] for n in nodes}
    fs_tokens = {}

    ### --nodes narrows the work to specific target nodes, which is what makes
    ### resuming a partially finished restore possible
    wanted_nodes = {n.strip() for n in args.nodes.split(",")} if args.nodes else None
    if wanted_nodes:
        known = {n["id"] for shard in nodes_by_shard.values() for n in shard}
        unknown = sorted(wanted_nodes - known)
        if unknown:
            raise SystemExit(
                f"--nodes names node(s) not in the mapped shards of "
                f"'{args.target_cluster}': {', '.join(unknown)}"
            )
        nodes_by_shard = {
            shard: [n for n in members if n["id"] in wanted_nodes]
            for shard, members in nodes_by_shard.items()
        }
        logger.info(f"Restricted to node(s): {', '.join(sorted(wanted_nodes))}")

    ### only unmount what is about to be worked on, so a resume leaves the nodes
    ### that already finished mounted and untouched
    selected = [n for shard in shard_mapping.values() for n in nodes_by_shard.get(shard, [])]
    if not selected:
        raise SystemExit("No target nodes selected for restore")

    ### the mount point comes from the shard's own host_metadata rather than
    ### being assumed uniform, and the hypervisors come from the map
    target_hosts = [n["id"].split(":")[0] for n in selected]
    kvm_hosts = sorted({
        infra.guests[h]["hypervisor"]
        for h in target_hosts
        if h in infra.guests
        and infra.guests[h].get("hypervisor")
        and infra.guests[h]["hypervisor"].lower() != "none"
    })
    unmount_points = sorted({
        host_metadata[s][0]["mount_point"]
        for s, t in shard_mapping.items()
        if nodes_by_shard.get(t)
    })

    logger.info(f"Unmounting MongoDB data volumes on: {', '.join(target_hosts)}")
    for unmount_point in unmount_points:
        logged(unmount_hosts, mdb_hosts=target_hosts, kvm_hosts=kvm_hosts, mount_point=unmount_point)
    logger.info("Target nodes unmounted")
    record_opsman_step(
        backup_run_dir, metadata, "unmount_targets",
        mount_points=unmount_points, hypervisors=kvm_hosts,
    )

    for source_shard, target_shard in shard_mapping.items():
        source_entry = host_metadata[source_shard][0]

        context = {
            "backup_run_dir": backup_run_dir,
            "metadata": metadata,
            "infra": infra,
            "tokens": fs_tokens,
            "host_metadata": host_metadata,
            "target_cluster": args.target_cluster,
            "mdb_snapshot_id": run["snapshot_id"],
            "snapshot_name": snapshots[source_shard],
            "mount_point": source_entry["mount_point"],
            "source_fs_uuid": source_entry["fs_uuid"],
        }

        logger.info(
            f"=== shard {source_shard} -> {target_shard} from snapshot "
            f"{context['snapshot_name']} ==="
        )

        for node in nodes_by_shard.get(target_shard, []):
            node_id = node["id"]
            node_record = node_record_for(
                metadata, node_id, target_shard, source_shard, context["snapshot_name"]
            )
            save_restore_metadata(backup_run_dir, metadata)

            logger.info(f"--- restoring {node_id} ---")

            try:
                restore_node_storage(node, context, node_record)
            except RuntimeError as e:
                node_record["status"] = "failed"
                node_record["error"] = str(e)
                metadata["status"] = "failed"
                metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
                save_restore_metadata(backup_run_dir, metadata)
                logger.error(f"{node_id}: {e}")
                raise

            ### only mark the node once Ops Manager is expecting it
            if wait_for_node_state(
                args.base_url, args.group_id, restore_id, node_id, "COPY_FILES", auth,
                args.poll_interval,
            ):
                logged(
                    files_copied, args.base_url, args.group_id, restore_id,
                    node_id, db_paths.get(node_id, node["db_path"]), auth,
                )
                record_node_step(backup_run_dir, metadata, node_record, "files_copied")
                record_opsman_step(backup_run_dir, metadata, "files_copied", node=node_id)
            else:
                node_record["status"] = "failed"
                node_record["error"] = "node never reached COPY_FILES"
                metadata["status"] = "failed"
                save_restore_metadata(backup_run_dir, metadata)
                raise RuntimeError(f"{node_id} never reached COPY_FILES")

            node_record["status"] = "completed"
            save_restore_metadata(backup_run_dir, metadata)


    while(state != "COMPLETED"):
        status = poll_restore(args.base_url, args.group_id, restore_id, auth)
        state = status.get("state")
        logger.info(f"Restore state: {state}")
        time.sleep(args.poll_interval)
    
    record_opsman_step(backup_run_dir, metadata, "state_completed")
    metadata["status"] = "completed"
    metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
    logger.info(f"Restore record: {save_restore_metadata(backup_run_dir, metadata)}")

    logger.info("Restore completed successfully")
        

if __name__ == "__main__":
    main()
    
