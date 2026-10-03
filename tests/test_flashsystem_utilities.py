#!/usr/bin/env python3

# ####################################################################################
#
# Purpose : Parent test utility for ibmflashsystem/flashsystem_utilities.py.
#           Exercises the FlashSystem helpers in that module against a live
#           FlashSystem, one function per --check. As flashsystem_utilities.py
#           grows new functions, add a matching --check here rather than
#           writing a new script.
#
# Usage   : test_flashsystem_utilities.py [-h]
#                                          [--system FLASHSYSTEM]
#                                          [--hostname HOSTNAME]
#                                          [--search SEARCH]
#                                          [--snapshot-name SNAPSHOT_NAME]
#                                          [--snapshot-id SNAPSHOT_ID]
#                                          [--namestring NAMESTRING]
#                                          [--volume-group VOLUME_GROUP]
#                                          [--volume-name VOLUME_NAME]
#                                          [--volume-uuid VOLUME_UUID]
#                                          [--volume-id VOLUME_ID]
#                                          [--policy-name POLICY_NAME]
#                                          [--check token|find-host|list-snapshots|
#                                                   snapshot-volume-group|create-volume-group|
#                                                   list-volumes|map-volume|volume-uuid|
#                                                   volume-id-from-uuid|volume-group-from-id|
#                                                   dependent-volume-groups|replication-policy-target]
#
# NOTE    : --check create-volume-group and --check map-volume are NOT read-only.
#           create-volume-group calls mkvolumegroup and creates a real volume
#           group ("restore_" + --namestring) on the target FlashSystem.
#           map-volume calls mkvdiskhostmap and maps --volume-name to --hostname
#           on the target FlashSystem.
#
# ####################################################################################

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import flash_credentials as fs
from ibmflashsystem import flashsystem_utilities

# ---------------- Checks ----------------

def _get_token(system):
    entry = next((f for f in fs.FlashSystems if f["system"] == system), None)
    if entry is None:
        raise SystemExit(f"No credentials for FlashSystem '{system}' in flash_credentials.py")

    return flashsystem_utilities.getFlashSystemToken(entry["system"], entry["user"], entry["password"])

def check_token(args):
    token = _get_token(args.system)
    print(f"  {args.system} : {token}")
    return token

def check_find_host(args):
    token = _get_token(args.system)
    found = flashsystem_utilities.findHost(args.system, token, args.hostname)
    print(f"  {args.system} : host '{args.hostname}' found = {found}")
    return found

def check_list_snapshots(args):
    token = _get_token(args.system)
    matches = flashsystem_utilities.listSnapshots(args.system, token, args.search)
    if not matches:
        print(f"  {args.system} : no snapshots match '{args.search}'")
    else:
        print(f"  {args.system} : {len(matches)} snapshot(s) match '{args.search}'\n")
        for snap in matches:
            print(f"    {snap.get('name')}  (volume_group={snap.get('volume_group_name')})")
    return matches

def check_snapshot_volume_group(args):
    token = _get_token(args.system)
    result = flashsystem_utilities.getSnapshotVolumeGroup(args.system, token, args.snapshot_name)
    if not result:
        print(f"  {args.system} : no snapshot named '{args.snapshot_name}'")
    else:
        volume_group, snapshot_id = result
        print(f"  {args.system} : snapshot '{args.snapshot_name}' -> volume_group={volume_group}, id={snapshot_id}")
    return result

def check_create_volume_group(args):
    token = _get_token(args.system)
    result = flashsystem_utilities.createVolumeGroup(args.system, token, args.snapshot_id, args.namestring)
    print(f"  {args.system} : created volume group 'restore_{args.namestring}' from snapshot {args.snapshot_id}")
    print(f"  response: {result}")
    return result

def check_list_volumes(args):
    token = _get_token(args.system)
    names = flashsystem_utilities.listVolumeGroupVolumes(args.system, token, args.volume_group)
    if not names:
        print(f"  {args.system} : no volumes in volume group '{args.volume_group}'")
    else:
        print(f"  {args.system} : {len(names)} volume(s) in '{args.volume_group}'\n")
        for name in names:
            print(f"    {name}")
    return names

def check_map_volume(args):
    token = _get_token(args.system)
    result = flashsystem_utilities.mapVolumeToHost(args.system, token, args.volume_name, args.hostname)
    print(f"  {args.system} : mapped volume '{args.volume_name}' to host '{args.hostname}'")
    print(f"  response: {result}")
    return result

def check_volume_uuid(args):
    token = _get_token(args.system)
    uuid = flashsystem_utilities.getVolumeUUID(args.system, token, args.volume_name)
    if not uuid:
        print(f"  {args.system} : no volume named '{args.volume_name}'")
    else:
        print(f"  {args.system} : volume '{args.volume_name}' -> UUID {uuid}")
    return uuid

def check_volume_id_from_uuid(args):
    token = _get_token(args.system)
    vol_id = flashsystem_utilities.getVolumeIDfromUUID(args.system, token, args.volume_uuid)
    if not vol_id:
        print(f"  {args.system} : no volume with UUID '{args.volume_uuid}'")
    else:
        print(f"  {args.system} : UUID '{args.volume_uuid}' -> id {vol_id}")
    return vol_id

def check_volume_group_from_id(args):
    token = _get_token(args.system)
    volume_group = flashsystem_utilities.getVolumeGroupFromVolumeID(args.system, token, args.volume_id)
    if not volume_group:
        print(f"  {args.system} : no volume with id '{args.volume_id}'")
    else:
        print(f"  {args.system} : volume id '{args.volume_id}' -> volume_group {volume_group}")
    return volume_group

def check_dependent_volume_groups(args):
    token = _get_token(args.system)
    matches = flashsystem_utilities.listDependentVolumeGroups(args.system, token, args.volume_group)
    if not matches:
        print(f"  {args.system} : no volume groups depend on '{args.volume_group}'")
    else:
        print(f"  {args.system} : {len(matches)} volume group(s) depend on '{args.volume_group}'\n")
        for name, policy_name in matches:
            print(f"    {name}  (policy={policy_name})")
    return matches

def check_replication_policy_target(args):
    token = _get_token(args.system)
    target = flashsystem_utilities.getReplicationPolicyTargetLocation(args.system, token, args.policy_name)
    if not target:
        print(f"  {args.system} : no replication policy named '{args.policy_name}'")
    else:
        print(f"  {args.system} : replication policy '{args.policy_name}' -> target {target}")
    return target

CHECKS = {
    "token": check_token,
    "find-host": check_find_host,
    "list-snapshots": check_list_snapshots,
    "snapshot-volume-group": check_snapshot_volume_group,
    "create-volume-group": check_create_volume_group,
    "list-volumes": check_list_volumes,
    "map-volume": check_map_volume,
    "volume-uuid": check_volume_uuid,
    "volume-id-from-uuid": check_volume_id_from_uuid,
    "volume-group-from-id": check_volume_group_from_id,
    "dependent-volume-groups": check_dependent_volume_groups,
    "replication-policy-target": check_replication_policy_target,
}

# ---------------- MAIN ----------------

def main():
    parser = argparse.ArgumentParser(description="Test utility for ibmflashsystem/flashsystem_utilities.py")
    parser.add_argument("--system", default="fs9500-2.cpolab.ibm.com")
    parser.add_argument("--hostname", default="SG-FCP-ORION15-mdb7")
    parser.add_argument("--search", default="6a8742e13d9e727fecb421a7")
    parser.add_argument("--snapshot-name", default="ORION16-mdb8_6a8742e13d9e727fecb421a7")
    parser.add_argument("--snapshot-id", default="17")
    parser.add_argument("--namestring", default="rmdb8_6a8742e13d9e727fecb421a7")
    parser.add_argument("--volume-group", default="ORION15-mdb7")
    parser.add_argument("--volume-name", default="ORION15-mdb7-shard2-2")
    parser.add_argument("--volume-uuid", default="60050768138202df88000000000000f1")
    parser.add_argument("--volume-id", default="235")
    parser.add_argument("--policy-name", default="ReplicatedSnaps")
    parser.add_argument("--check", choices=sorted(CHECKS), default="token")
    parser.add_argument("--json", action="store_true", help="Print raw result as JSON instead")
    args = parser.parse_args()

    print(f"\n--check {args.check} for system: {args.system}\n")
    result = CHECKS[args.check](args)

    if args.json:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
