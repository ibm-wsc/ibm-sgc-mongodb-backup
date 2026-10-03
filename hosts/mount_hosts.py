import logging
import subprocess
import threading
import time

from restore.node_restore_map import node_mappings

logger = logging.getLogger("opsmgr_restore")

# ---------------- UNMOUNT DEFAULTS ----------------

MDB_HOSTS = [
    "rmdb1", "rmdb2", "rmdb3", "rmdb4",
    "rmdb-config1", "rmdb-config2",
    "rmdb7", "rmdb8", "rmdb9",
]
KVM_HOSTS = ["mdb-kvm1", "mdb-kvm2", "mdb-kvm3"]
MOUNT_POINT = "/fimongo1"
RETRY_DELAY = 10


# ---------------- UNMOUNT ----------------

def _ssh_umount(host, mount_point):
    result = subprocess.run(
        ["ssh", host, f"umount {mount_point}"],
        capture_output=True,
        text=True,
    )
    return (result.stdout + result.stderr).lower()


def _drop_kvm_caches(host):
    logger.info(f"Resolving KVM cache on host: {host}")
    subprocess.run(
        ["ssh", host, "sync && echo 3 > /proc/sys/vm/drop_caches"],
        capture_output=True,
        text=True,
    )


def unmount_hosts(
    mdb_hosts=MDB_HOSTS,
    kvm_hosts=KVM_HOSTS,
    mount_point=MOUNT_POINT,
    retry_delay=RETRY_DELAY,
):
    """Unmount mount_point from all mdb_hosts, retrying until none are busy,
    then flush KVM page caches in parallel."""
    while True:
        busy = []
        for host in mdb_hosts:
            output = _ssh_umount(host, mount_point)
            if "busy" in output:
                logger.info(f"Host {host} is currently busy")
                busy.append(host)

        logger.info(f"Busy hosts: {len(busy)}")

        if not busy:
            break

        time.sleep(retry_delay)

    for host in mdb_hosts:
        logger.info(f"Flushing cache after umount on {host}")
        subprocess.run(
            ["ssh", host, "sync && echo 3 > /proc/sys/vm/drop_caches"],
            capture_output=True,
            text=True,
        )

    for host in kvm_hosts:
        _drop_kvm_caches(host)
        
   


# ---------------- MOUNT ----------------

def _find_node_in_metadata(host_metadata, source_id):
    """Return the node entry for source_id across all shards, or None."""
    for nodes in host_metadata.values():
        for node in nodes:
            if node["hostname"] == source_id:
                return node
    return None


def mount_host_filesystems(host_metadata, dry_run=False):
    """SSH to each target host and mount the filesystem UUID from its source node."""

    def _mount(target_host, fs_uuid, mount_point):
        if dry_run:
            logger.info(f"[DRY RUN] Would run: ssh {target_host} mount UUID={fs_uuid} {mount_point}")
            return
        logger.info(f"Mounting UUID={fs_uuid} on {target_host} at {mount_point}")
        result = subprocess.run(
            ["ssh", target_host, f"mount UUID={fs_uuid} {mount_point}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(f"Mount failed on {target_host}: {result.stderr.strip()}")
            return
        logger.info(f"Mounted successfully on {target_host}")
        chown = subprocess.run(
            ["ssh", target_host, f"chown -R mongod:mongod {mount_point}"],
            capture_output=True,
            text=True,
        )
        if chown.returncode != 0:
            logger.warning(f"chown failed on {target_host}: {chown.stderr.strip()}")
        else:
            logger.info(f"chown mongod:mongod applied on {target_host} at {mount_point}")

    threads = []
    for target_id, source_id in node_mappings.items():
        node = _find_node_in_metadata(host_metadata, source_id)
        if not node:
            logger.warning(f"No host_metadata entry for source {source_id}, skipping mount on {target_id}")
            continue
        target_host = target_id.split(":")[0]
        threads.append(threading.Thread(
            target=_mount,
            args=(target_host, node["fs_uuid"], node["mount_point"]),
            daemon=True,
        ))

    for t in threads:
        t.start()
    for t in threads:
        t.join()


def mount_single_host(target_id, host_metadata, dry_run=False, fs_uuid=None, mount_point=None):
    """SSH to a single target host and mount a filesystem UUID at a mount point.

    `fs_uuid` and `mount_point` may be supplied by the caller, which is how the
    restore passes the UUID it assigned to a freshly cloned volume along with
    the mount point recorded for that shard. When either is omitted it is
    resolved the original way, through node_mappings into the backup's
    host_metadata.
    """
    target_host = target_id.split(":")[0]

    if fs_uuid is None or mount_point is None:
        source_id = node_mappings.get(target_id)
        if not source_id:
            logger.warning(f"No node_mappings entry for target {target_id}, skipping mount")
            return
        node = _find_node_in_metadata(host_metadata, source_id)
        if not node:
            logger.warning(f"No host_metadata entry for source {source_id}, skipping mount on {target_id}")
            return

        fs_uuid = fs_uuid if fs_uuid is not None else node["fs_uuid"]
        mount_point = mount_point if mount_point is not None else node["mount_point"]

    if dry_run:
        logger.info(f"[DRY RUN] Would run: ssh {target_host} mount UUID={fs_uuid} {mount_point}")
        return

    # logger.info(f"Flushing cache on {target_host}")
    # subprocess.run(
    #     ["ssh", target_host, "echo 3 > /proc/sys/vm/drop_caches"],
    #     capture_output=True,
    #     text=True,
    # )

    logger.info(f"Partprobe on {target_host} to detect filesystem updates")
    result = subprocess.run(
        ["ssh", target_host, f"partprobe"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning(f"Partprobe failed on {target_host}: {result.stderr.strip()}")
        return


    logger.info(f"Mounting UUID={fs_uuid} on {target_host} at {mount_point}")
    result = subprocess.run(
        ["ssh", target_host, f"mount UUID={fs_uuid} {mount_point}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning(f"Mount failed on {target_host}: {result.stderr.strip()}")
        return
    logger.info(f"Mounted successfully on {target_host}")

    chown = subprocess.run(
        ["ssh", target_host, f"chown -R mongod:mongod {mount_point}"],
        capture_output=True,
        text=True,
    )
    if chown.returncode != 0:
        logger.warning(f"chown failed on {target_host}: {chown.stderr.strip()}")
    else:
        logger.info(f"chown mongod:mongod applied on {target_host} at {mount_point}")


def verify_single_host_mount(target_id, host_metadata, mount_point=None):
    """Verify a single target host has its mount point active.

    `mount_point` may be supplied by the caller; when omitted it is resolved
    the original way, through node_mappings into the backup's host_metadata.
    Raises RuntimeError if the mount is absent."""
    target_host = target_id.split(":")[0]

    if mount_point is None:
        source_id = node_mappings.get(target_id)
        if not source_id:
            logger.warning(f"No node_mappings entry for target {target_id}, skipping verification")
            return
        node = _find_node_in_metadata(host_metadata, source_id)
        if not node:
            logger.warning(f"No host_metadata entry for source {source_id}, skipping verification of {target_id}")
            return

        mount_point = node["mount_point"]

    result = subprocess.run(
        ["ssh", target_host, f"mountpoint -q {mount_point}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Mount verification failed on host: {target_host} ({mount_point} not mounted)")
    logger.info(f"Mount verified on {target_host}: {mount_point}")

    filelist = f"{mount_point}/data/fileList.txt"
    check = subprocess.run(
        ["ssh", target_host, f"test -f {filelist} && stat -c '%U:%G' {filelist}"],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        raise RuntimeError(f"{target_host}: {filelist} does not exist")
    else:
        owner = check.stdout.strip()
        if owner != "mongod:mongod":
            logger.warning(f"{target_host}: {filelist} has incorrect ownership '{owner}' (expected mongod:mongod)")
        else:
            logger.info(f"{target_host}: {filelist} exists with correct mongod:mongod ownership")


# ---------------- VERIFY ----------------

def verify_host_mounts(host_metadata):
    """Verify every target host has its mount point active.
    Raises RuntimeError listing any hosts where the mount is absent."""

    failures = []
    lock = threading.Lock()

    def _check(target_host, mount_point):
        result = subprocess.run(
            ["ssh", target_host, f"mountpoint -q {mount_point}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(f"Mount verification FAILED on {target_host}: {mount_point} is not mounted")
            with lock:
                failures.append(target_host)
            return
        logger.info(f"Mount verified on {target_host}: {mount_point}")

        filelist = f"{mount_point}/data/fileList.txt"
        check = subprocess.run(
            ["ssh", target_host, f"test -f {filelist} && stat -c '%U:%G' {filelist}"],
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            logger.warning(f"{target_host}: {filelist} does not exist")
        else:
            owner = check.stdout.strip()
            if owner != "mongod:mongod":
                logger.warning(f"{target_host}: {filelist} has incorrect ownership '{owner}' (expected mongod:mongod)")
            else:
                logger.info(f"{target_host}: {filelist} exists with correct mongod:mongod ownership")

    threads = []
    for target_id, source_id in node_mappings.items():
        node = _find_node_in_metadata(host_metadata, source_id)
        if not node:
            logger.warning(f"No host_metadata entry for source {source_id}, skipping verification of {target_id}")
            continue
        target_host = target_id.split(":")[0]
        threads.append(threading.Thread(
            target=_check,
            args=(target_host, node["mount_point"]),
            daemon=True,
        ))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if failures:
        raise RuntimeError(f"Mount verification failed on hosts: {', '.join(failures)}")
