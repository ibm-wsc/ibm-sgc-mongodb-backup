import requests

### disable SSL verification
ssl_verify = False

### ignore warning for SSL not being used
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


def takeFlashSystemSnapshot(flashSystem, token, volumeGroup, mdbSnapshotID, shardName = None):
    """Take one snapshot of `volumeGroup` on `flashSystem`, called once per
    backup node. `token` is an auth token for that FlashSystem, obtained by
    the caller.

    The snapshot is named {shardName}_{mdbSnapshotID}. Only one node per shard
    is backed up, so the shard name alone keeps the name unique on the array.
    Without a shard name it falls back to {volumeGroup}_{mdbSnapshotID}.

    Returns the result entry recorded in the snapshot metadata.
    """
    snap_name = (shardName if shardName else volumeGroup) + "_" + mdbSnapshotID

    dataParameter = {
        'volumegroup': volumeGroup,
        'name' : snap_name,
        'safeguarded': False,
        'retentiondays': 14
        }

    addSnapshotRequest = requests.post('https://' + flashSystem + ':7443/rest/v1/addsnapshot',
        headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
        },
        params="", json=dataParameter, verify=ssl_verify)

    result = addSnapshotRequest.json()
    print(flashSystem, "Volume Group", volumeGroup, "Shard", shardName, result)

    return {
        "flashsystem": flashSystem,
        "volume_group": volumeGroup,
        "shard": shardName,
        "snapshot_name": snap_name,
        "response": result
    }

### The remote (replication target) snapshot is the same addsnapshot call
### against the remote array's volume group, so takeFlashSystemSnapshot above
### serves both and the local and remote names stay identical.