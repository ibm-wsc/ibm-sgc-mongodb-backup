import requests

### disable SSL verification
ssl_verify = False

### ignore warning for SSL not being used
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

def getFlashSystemToken(system, user, password):
    """Request an auth token for a single FlashSystem (system, user, password)."""
    try:
        tokenRequest = requests.post('https://' + system + ':7443/rest/v1/auth',
            headers={
            'Content-type': 'application/json',
            'X-Auth-Username': user,
            'X-Auth-Password': password
            },
            params="", data="", verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    try:
        tokenRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' auth failed: HTTP {tokenRequest.status_code} {tokenRequest.reason}"
        ) from e

    try:
        return tokenRequest.json()['token']
    except (ValueError, KeyError) as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected auth response") from e


def findHost(system, token, hostname):
    """Look up a host object by name on a FlashSystem via lshost.

    Returns True if a host named `hostname` is found, False if the query
    succeeds but finds no match. Raises RuntimeError on request failures,
    including an expired/invalid token (HTTP 401/403).
    """
    try:
        hostRequest = requests.post('https://' + system + ':7443/rest/v1/lshost',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={'filtervalue': 'name=' + hostname},
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if hostRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the lshost request: "
            f"HTTP {hostRequest.status_code} {hostRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        hostRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' lshost query failed: HTTP {hostRequest.status_code} {hostRequest.reason}"
        ) from e

    try:
        hosts = hostRequest.json()
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected lshost response") from e

    return bool(hosts)


def listSnapshots(system, token, search):
    """List volume group snapshots on a FlashSystem whose name contains `search`.

    Returns the list of matching snapshot objects, or False if none match.
    Raises RuntimeError on request failures, including an expired/invalid
    token (HTTP 401/403).
    """
    try:
        snapRequest = requests.post('https://' + system + ':7443/rest/v1/lsvolumegroupsnapshot',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={},
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if snapRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the lsvolumegroupsnapshot request: "
            f"HTTP {snapRequest.status_code} {snapRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        snapRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' lsvolumegroupsnapshot query failed: "
            f"HTTP {snapRequest.status_code} {snapRequest.reason}"
        ) from e

    try:
        snapshots = snapRequest.json()
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected lsvolumegroupsnapshot response") from e

    matches = [s for s in snapshots if search in s.get('name', '')]

    return matches if matches else False


def getSnapshotVolumeGroup(system, token, snapshot_name):
    """Look up a snapshot by exact name and return its volume group and snapshot id.

    Returns a (volume_group_name, snapshot_id) tuple, or False if no snapshot
    is named exactly `snapshot_name`. Raises RuntimeError on request failures,
    including an expired/invalid token (HTTP 401/403).
    """
    try:
        snapRequest = requests.post('https://' + system + ':7443/rest/v1/lsvolumegroupsnapshot',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={'filtervalue': 'name=' + snapshot_name},
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if snapRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the lsvolumegroupsnapshot request: "
            f"HTTP {snapRequest.status_code} {snapRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        snapRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' lsvolumegroupsnapshot query failed: "
            f"HTTP {snapRequest.status_code} {snapRequest.reason}"
        ) from e

    try:
        snapshots = snapRequest.json()
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected lsvolumegroupsnapshot response") from e

    if not snapshots:
        return False

    match = snapshots[0]
    return match['volume_group_name'], match['id']


def createVolumeGroup(system, token, snapshotid, namestring):
    """Create a thin volume group on a FlashSystem from an existing snapshot via mkvolumegroup.

    Returns the API response on success. Raises RuntimeError on request
    failures, including an expired/invalid token (HTTP 401/403).
    """
    try:
        mkRequest = requests.post('https://' + system + ':7443/rest/v1/mkvolumegroup',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={
                'type': 'thinclone',
                'fromsnapshotid': snapshotid,
                'name': 'restore_' + namestring                
            },
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if mkRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the mkvolumegroup request: "
            f"HTTP {mkRequest.status_code} {mkRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        mkRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' mkvolumegroup failed: HTTP {mkRequest.status_code} {mkRequest.reason}"
        ) from e

    try:
        return mkRequest.json()
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected mkvolumegroup response") from e


def restoreVolumeGroupName(node_name, target_cluster, mdb_snapshot_id):
    """Name for a restore thin clone volume group: {node}_{cluster}_{snapshot}.

    `node_name` may be a full node id ('rmdb7:37017'); only the host part is
    used, since a colon is not valid in a FlashSystem object name.
    """
    return f"{node_name.split(':')[0]}_{target_cluster}_{mdb_snapshot_id}"


def createThinCloneVolumeGroup(system, token, snapshot_id, volume_group_name):
    """Create a thin clone volume group from an existing snapshot via
    mkvolumegroup, named exactly `volume_group_name`.

    `snapshot_id` is the FlashSystem snapshot id (as returned by
    getSnapshotVolumeGroup), not the MongoDB snapshot id.

    Returns the API response on success. Raises RuntimeError on request
    failures, including an expired/invalid token (HTTP 401/403), carrying the
    array's own message so a refusal is readable.
    """
    try:
        mkRequest = requests.post('https://' + system + ':7443/rest/v1/mkvolumegroup',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={
                'type': 'thinclone',
                'fromsnapshotid': snapshot_id,
                'name': volume_group_name
            },
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if mkRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the mkvolumegroup request: "
            f"HTTP {mkRequest.status_code} {mkRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        mkRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' mkvolumegroup failed for thin clone "
            f"'{volume_group_name}' from snapshot id {snapshot_id}: "
            f"HTTP {mkRequest.status_code} {mkRequest.reason}: {mkRequest.text.strip()}"
        ) from e

    try:
        return mkRequest.json() if mkRequest.text else {}
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected mkvolumegroup response") from e


def renameVolume(system, token, volume_id, new_name):
    """Rename a volume via chvdisk.

    `volume_id` is the vdisk id, as returned by lsvdisk. Returns the API
    response on success. Raises RuntimeError on request failures, including an
    expired/invalid token (HTTP 401/403), carrying the array's own message.
    """
    try:
        chRequest = requests.post('https://' + system + ':7443/rest/v1/chvdisk/' + str(volume_id),
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={'name': new_name},
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if chRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the chvdisk request: "
            f"HTTP {chRequest.status_code} {chRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        chRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' chvdisk failed renaming volume {volume_id} "
            f"to '{new_name}': HTTP {chRequest.status_code} {chRequest.reason}: "
            f"{chRequest.text.strip()}"
        ) from e

    try:
        return chRequest.json() if chRequest.text else {}
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected chvdisk response") from e


def renameVolumeGroupVolumes(system, token, volume_group_name, base_name):
    """Rename every volume in `volume_group_name` to `base_name`.

    A volume group holding one volume, which is the case for these MongoDB
    dbPath volume groups, gets exactly `base_name`. If it holds several they
    are suffixed -1, -2 and so on so the names stay unique.

    Returns a list of (old_name, new_name) pairs.
    """
    names = listVolumeGroupVolumes(system, token, volume_group_name)
    if not names:
        raise RuntimeError(
            f"FlashSystem '{system}': volume group '{volume_group_name}' holds no volumes to rename"
        )

    renamed = []

    for index, old_name in enumerate(sorted(names), start=1):
        new_name = base_name if len(names) == 1 else f"{base_name}-{index}"

        volume_id = _getVolumeId(system, token, old_name)
        if volume_id is None:
            raise RuntimeError(f"FlashSystem '{system}': no volume named '{old_name}'")

        renameVolume(system, token, volume_id, new_name)
        renamed.append((old_name, new_name))

    return renamed


def listVolumeGroupVolumes(system, token, volume_group_name):
    """List volume names belonging to a volume group on a FlashSystem via lsvdisk.

    Returns the list of volume names, or False if the volume group has no
    volumes (or doesn't exist). Raises RuntimeError on request failures,
    including an expired/invalid token (HTTP 401/403).
    """
    try:
        volRequest = requests.post('https://' + system + ':7443/rest/v1/lsvdisk',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={'filtervalue': 'volume_group_name=' + volume_group_name},
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if volRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the lsvdisk request: "
            f"HTTP {volRequest.status_code} {volRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        volRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' lsvdisk query failed: HTTP {volRequest.status_code} {volRequest.reason}"
        ) from e

    try:
        volumes = volRequest.json()
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected lsvdisk response") from e

    names = [v['name'] for v in volumes]

    return names if names else False


def _getVolumeId(system, token, volume_name):
    """Look up a volume's id by exact name via lsvdisk. Returns None if not found."""
    try:
        volRequest = requests.post('https://' + system + ':7443/rest/v1/lsvdisk',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={'filtervalue': 'name=' + volume_name},
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if volRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the lsvdisk request: "
            f"HTTP {volRequest.status_code} {volRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        volRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' lsvdisk query failed: HTTP {volRequest.status_code} {volRequest.reason}"
        ) from e

    try:
        volumes = volRequest.json()
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected lsvdisk response") from e

    return volumes[0]['id'] if volumes else None


def mapVolumeToHost(system, token, volume_name, host_name):
    """Map a volume to a host on a FlashSystem via mkvdiskhostmap.

    Returns the API response on success. Raises RuntimeError on request
    failures, including an expired/invalid token (HTTP 401/403), or if
    `volume_name` doesn't resolve to an existing volume.
    """
    volume_id = _getVolumeId(system, token, volume_name)
    if volume_id is None:
        raise RuntimeError(f"FlashSystem '{system}': no volume named '{volume_name}'")

    try:
        mapRequest = requests.post('https://' + system + ':7443/rest/v1/mkvdiskhostmap/' + volume_id,
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={
                'host': host_name
            },
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if mapRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the mkvdiskhostmap request: "
            f"HTTP {mapRequest.status_code} {mapRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        mapRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' mkvdiskhostmap failed: HTTP {mapRequest.status_code} {mapRequest.reason}"
        ) from e

    try:
        return mapRequest.json()
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected mkvdiskhostmap response") from e


def getVolumeUUID(system, token, volume_name):
    """Look up a volume's UUID by exact name on a FlashSystem via lsvdisk.

    Returns the volume's UUID as a lowercase string, or False if no volume is
    named exactly `volume_name`. Raises RuntimeError on request failures,
    including an expired/invalid token (HTTP 401/403).
    """
    try:
        volRequest = requests.post('https://' + system + ':7443/rest/v1/lsvdisk',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={'filtervalue': 'name=' + volume_name},
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if volRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the lsvdisk request: "
            f"HTTP {volRequest.status_code} {volRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        volRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' lsvdisk query failed: HTTP {volRequest.status_code} {volRequest.reason}"
        ) from e

    try:
        volumes = volRequest.json()
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected lsvdisk response") from e

    if not volumes:
        return False

    return volumes[0]['vdisk_UID'].lower()


def getVolumeIDfromUUID(system, token, volume_uuid):
    """Look up a volume's id by its UUID (vdisk_UID) on a FlashSystem via lsvdisk.

    Returns the volume's id, or False if no volume matches `volume_uuid`.
    Raises RuntimeError on request failures, including an expired/invalid
    token (HTTP 401/403).
    """
    try:
        volRequest = requests.post('https://' + system + ':7443/rest/v1/lsvdisk',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={'filtervalue': 'vdisk_UID=' + volume_uuid},
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if volRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the lsvdisk request: "
            f"HTTP {volRequest.status_code} {volRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        volRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' lsvdisk query failed: HTTP {volRequest.status_code} {volRequest.reason}"
        ) from e

    try:
        volumes = volRequest.json()
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected lsvdisk response") from e

    return volumes[0]['id'] if volumes else False


def getVolumeGroupFromVolumeID(system, token, volume_id):
    """Look up the volume group a volume belongs to on a FlashSystem via lsvdisk.

    Returns the volume_group_name, or False if no volume matches `volume_id`.
    Raises RuntimeError on request failures, including an expired/invalid
    token (HTTP 401/403).
    """
    try:
        volRequest = requests.post('https://' + system + ':7443/rest/v1/lsvdisk',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={'filtervalue': 'id=' + str(volume_id)},
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if volRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the lsvdisk request: "
            f"HTTP {volRequest.status_code} {volRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        volRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' lsvdisk query failed: HTTP {volRequest.status_code} {volRequest.reason}"
        ) from e

    try:
        volumes = volRequest.json()
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected lsvdisk response") from e

    return volumes[0]['volume_group_name'] if volumes else False


def listDependentVolumeGroups(system, token, volume_group_name):
    """List volume groups that were thin-cloned from a snapshot of
    `volume_group_name` on a FlashSystem and have a replication policy
    assigned, via lsvolumegroup.

    Returns a list of (name, replication_policy_name) tuples, or False if
    none exist. Raises RuntimeError on request failures, including an
    expired/invalid token (HTTP 401/403).
    """
    try:
        vgRequest = requests.post('https://' + system + ':7443/rest/v1/lsvolumegroup',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={'filtervalue': 'source_volume_group_name=' + volume_group_name},
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if vgRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the lsvolumegroup request: "
            f"HTTP {vgRequest.status_code} {vgRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        vgRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' lsvolumegroup query failed: HTTP {vgRequest.status_code} {vgRequest.reason}"
        ) from e

    try:
        volume_groups = vgRequest.json()
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected lsvolumegroup response") from e

    matches = [(vg['name'], vg.get('replication_policy_name')) for vg in volume_groups if vg.get('replication_policy_id')]

    return matches if matches else False


def isVolumeGroupReplicated(system, token, volume_group_name):
    """Check whether a volume group on a FlashSystem has a replication
    policy assigned, via lsvolumegroup.

    Returns True/False. Raises RuntimeError on request failures, including
    an expired/invalid token (HTTP 401/403).
    """
    try:
        vgRequest = requests.post('https://' + system + ':7443/rest/v1/lsvolumegroup',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={'filtervalue': 'name=' + volume_group_name},
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if vgRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the lsvolumegroup request: "
            f"HTTP {vgRequest.status_code} {vgRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        vgRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' lsvolumegroup query failed: HTTP {vgRequest.status_code} {vgRequest.reason}"
        ) from e

    try:
        volume_groups = vgRequest.json()
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected lsvolumegroup response") from e

    return bool(volume_groups) and bool(volume_groups[0].get('replication_policy_id'))


def getReplicationPolicyTargetLocation(system, token, policy_name):
    """Look up a replication policy by exact name on a FlashSystem via
    lsreplicationpolicy and return the name of the *other* location, i.e.
    the replication target relative to `system`.

    Returns the target system name (e.g. 'FS9500-3'), or False if no policy
    is named exactly `policy_name`. Raises RuntimeError on request failures,
    including an expired/invalid token (HTTP 401/403), or if neither of the
    policy's two locations matches `system`.
    """
    try:
        policyRequest = requests.post('https://' + system + ':7443/rest/v1/lsreplicationpolicy',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={'filtervalue': 'name=' + policy_name},
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if policyRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the lsreplicationpolicy request: "
            f"HTTP {policyRequest.status_code} {policyRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        policyRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' lsreplicationpolicy query failed: "
            f"HTTP {policyRequest.status_code} {policyRequest.reason}"
        ) from e

    try:
        policies = policyRequest.json()
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected lsreplicationpolicy response") from e

    if not policies:
        return False

    policy = policies[0]
    location1 = policy.get('location1_system_name', '')
    location2 = policy.get('location2_system_name', '')
    short_name = system.split('.')[0].lower()

    if short_name == location1.lower():
        return location2
    if short_name == location2.lower():
        return location1

    raise RuntimeError(
        f"FlashSystem '{system}' replication policy '{policy_name}' has locations "
        f"'{location1}'/'{location2}', neither of which matches '{system}'"
    )


def _refreshFromSnapshot(system, token, target_group, snapshot_name, source_volume_group):
    """Issue one refreshfromsnapshot and return the parsed response.

    A refusal carries the array's own message, which is what says why. The
    common one is CMMVC9934E, meaning the volumes in `target_group` are no
    longer the size they were when the snapshot was taken, usually because the
    source volumes have since been grown. That is a permanent condition: the
    clone has to be rebuilt or resized, so it is reported rather than retried.
    """
    try:
        refreshRequest = requests.post('https://' + system + ':7443/rest/v1/refreshfromsnapshot',
            headers={
            'Content-type': 'application/json',
            'X-Auth-token': token
            },
            json={
                'volumegroup': target_group,
                'snapshot': snapshot_name,
                'fromsourcegroup': source_volume_group
            },
            verify=ssl_verify, timeout=30)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to reach FlashSystem '{system}': {e}") from e

    if refreshRequest.status_code in (401, 403):
        raise RuntimeError(
            f"FlashSystem '{system}' rejected the refreshfromsnapshot request: "
            f"HTTP {refreshRequest.status_code} {refreshRequest.reason} "
            f"(token may be expired or invalid)"
        )

    try:
        refreshRequest.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"FlashSystem '{system}' refreshfromsnapshot failed for volume group "
            f"'{target_group}' from snapshot '{snapshot_name}': "
            f"HTTP {refreshRequest.status_code} {refreshRequest.reason}: "
            f"{refreshRequest.text.strip()}"
        ) from e

    try:
        return refreshRequest.json() if refreshRequest.text else {}
    except ValueError as e:
        raise RuntimeError(f"FlashSystem '{system}' returned an unexpected refreshfromsnapshot response") from e


def replicateFlashSystemSnapshot(system, token, source_volume_group, snapshot_name, dependents):
    """Refresh every dependent volume group replicating from
    `source_volume_group` to the snapshot named `snapshot_name`, via
    refreshfromsnapshot.

    `dependents` is the list of replicated clones the infrastructure map
    recorded for that volume group, each a dict with 'name' (the dependent
    volume group to refresh), 'policy' and 'target'. A source volume group may
    have several, and each is refreshed from the same snapshot. A dependent
    whose policy target did not resolve is still refreshed, since the clone and
    its replication policy both exist either way.

    Returns a list of result entries, one per dependent refreshed. Raises
    RuntimeError on request failures, including an expired/invalid token
    (HTTP 401/403), carrying the array's own message so a refusal such as
    CMMVC9934E (clone volumes resized since the snapshot) is readable.
    """
    results = []

    for dependent in dependents:
        target_group = dependent['name']

        response = _refreshFromSnapshot(
            system, token, target_group, snapshot_name, source_volume_group,
        )

        results.append({
            "flashsystem": system,
            "source_volume_group": source_volume_group,
            "volume_group": target_group,
            "policy": dependent.get('policy'),
            "target": dependent.get('target'),
            "snapshot_name": snapshot_name,
            "response": response,
        })

    return results

