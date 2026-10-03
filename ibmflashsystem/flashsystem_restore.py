import flash_credentials as fs
import ibmflashsystem.flashsystem_volumegroupsrestore_map as vgs
import argparse

import json
import time
import requests

### disable SSL verification
ssl_verify = False

### ignore warning for SSL not being used
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


def flashsystem_snapshot_name_from_mongo_id(system, token, volume_group, mongo_id):
    
    r = requests.post(
        f"https://{system}:7443/rest/v1/lsvolumegroupsnapshot",
        headers={
            "Content-Type": "application/json",
            "X-Auth-token": token,
        },
        json={
            
        },
        verify=False,
        timeout=30,
    )
    #"filtervalue" : 'volume_group_name='+volume_group
    r.raise_for_status()

    snapshots = r.json()   # list

    for snap in snapshots:
        if (
            mongo_id in snap.get("name") 
            and
            snap.get("volume_group_name") == volume_group
        ):
            # logger.info(
            #     f"Valid FlashSystem snapshot found: {snap.get("name")} "
            #     f"(VG={volume_group}, system={system})"
            # )
            return snap.get("name")

    # logger.info(
    #     f"Expired snapshot: {snapshot_name} "
    #     f"(VG={volume_group}, system={system})"
    # )
    # return False




### DEPRECATED
###
### Driven by the static restoreVolumeGroups map, which names source volume
### groups that predate the per-cluster rebuild, so it will not find the
### snapshots current backups create. Replace with a lookup built from the
### infrastructure map, the way the backup side now resolves volume groups.
def refreshFlashSystemRecoveryPoints(mdbSnapshotID = "202601011200", delay = 0):
    tokenRequests = dict()
    snapshot_results = []
    ### get session token for each FlashSystem
    for _flashSystem in fs.FlashSystems:
        tokenRequest = requests.post('https://' + _flashSystem['system'] + ':7443/rest/v1/auth',
            headers={
            'Content-type': 'application/json',
            'X-Auth-Username': _flashSystem['user'],
            'X-Auth-Password': _flashSystem['password']
            },
            params="", data="", verify=ssl_verify)

        ### convert to JSON
        _token = json.loads(tokenRequest.text)
        tokenRequests[_flashSystem['system']] = _token

    
    for _flashSystem in vgs.restoreVolumeGroups.keys():
        for _targetGroup in vgs.restoreVolumeGroups[_flashSystem].keys():
            
            sourceGroup = vgs.restoreVolumeGroups[_flashSystem][_targetGroup]
            snap_name = flashsystem_snapshot_name_from_mongo_id(_flashSystem,
                                                                tokenRequests[_flashSystem]['token'],
                                                                sourceGroup,
                                                                mdbSnapshotID
                                                                )
            
            dataParameter = {
                'volumegroup': _targetGroup,
                'snapshot' : snap_name,
                'fromsourcegroup': sourceGroup
                }

            refreshFromSnapshotRequest = requests.post('https://' + _flashSystem + ':7443/rest/v1/refreshfromsnapshot',
                headers={
                    'Content-type': 'application/json',
                    'X-Auth-token': tokenRequests[_flashSystem]['token']
                },
                params="", json=dataParameter, verify=ssl_verify)

          
            _volumeGroupRequest = json.loads(refreshFromSnapshotRequest.text)
            print(_flashSystem, "Refreshed Replicated Volume Group", _targetGroup, _volumeGroupRequest)

            result = refreshFromSnapshotRequest.json()

            time.sleep(delay)

def main():

    parser = argparse.ArgumentParser(description="FlashSystem Volume Refresh")
    parser.add_argument("--snapshot-id")
    args = parser.parse_args()

    print("Running FlashSystem Recovery...")
    refreshFlashSystemRecoveryPoints(args.snapshot_id)


if __name__ == "__main__":
    main()
    
    

