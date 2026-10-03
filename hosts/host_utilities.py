#!/usr/bin/env python3

import logging
import subprocess
import time

SSH_CONNECT_TIMEOUT = 5

logger = logging.getLogger(__name__)

_SSH_OPTS = [
    "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
]


# ---------------- SCSI DEVICE SCAN ----------------

def scan_scsi_devices(host: str) -> bool:
    """SSH to a KVM guest and rescan every SCSI host so newly attached block
    devices (e.g. a freshly mapped FlashSystem volume) become visible.

    Returns True on success, False if the SSH command failed.
    """
    cmd = 'for x in `ls -d /sys/class/scsi_host/*`; do echo "- - -" > $x/scan; done'
    result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [host, cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.warning(f"{host}: SCSI scan failed — {result.stderr.strip()}")
        return False

    return True


# ---------------- MULTIPATH DEVICE LOOKUP ----------------

def find_multipath_device_from_scsi_uuid(host: str, uuid: str, retries: int = 0, delay: int = 5):
    """SSH to host and return the multipath alias whose WWID matches `uuid`.

    `uuid` may be given with a leading '*' wildcard (stripped before
    matching); the match is a case-insensitive substring check against each
    line of `multipath -l` output (WWIDs carry a leading digit that `uuid`
    typically omits), and the alias is cut from the line's first column.

    A freshly mapped volume does not appear the instant the mapping is made, so
    `retries` extra attempts `delay` seconds apart can be requested. The
    default is a single attempt, matching the original behaviour.

    Returns the multipath alias (e.g. 'mpathl'), or False if no match.
    """
    needle = uuid.lower().lstrip("*")

    for attempt in range(retries + 1):
        if attempt:
            time.sleep(delay)

        result = subprocess.run(
            ["ssh"] + _SSH_OPTS + [host, "multipath -l"],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            logger.warning(f"{host}: multipath -l failed — {result.stderr.strip()}")
            return False

        for line in result.stdout.splitlines():
            if needle in line.lower():
                return line.split()[0]

        if attempt < retries:
            logger.info(
                f"{host}: no multipath device for {needle} yet "
                f"(attempt {attempt + 1} of {retries + 1}), rescanning"
            )
            scan_scsi_devices(host)

    return False


# ---------------- FILESYSTEM UUID ----------------

REPLAY_MOUNT_POINT = "/tmp/mount"


def replay_filesystem_log(host: str, device: str, fstype: str = "xfs"):
    """Mount and immediately unmount `device` at /tmp/mount so a dirty journal
    is replayed.

    A snapshot taken while the filesystem was mounted carries an unreplayed
    log, and xfs_admin refuses to touch the UUID until it has been replayed.
    XFS is mounted with -o nouuid, because the snapshot's UUID still matches
    the volume it was cloned from, which may already be mounted on this host.

    Returns True when the mount and unmount both succeeded.
    """
    subprocess.run(
        ["ssh"] + _SSH_OPTS + [host, f"mkdir -p {REPLAY_MOUNT_POINT}"],
        capture_output=True,
        text=True,
    )

    options = "-o nouuid " if fstype == "xfs" else ""
    mount_result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [host, f"mount {options}{device} {REPLAY_MOUNT_POINT}"],
        capture_output=True,
        text=True,
    )

    if mount_result.returncode != 0:
        logger.error(
            f"{host}: could not mount {device} at {REPLAY_MOUNT_POINT} to replay its log — "
            f"{mount_result.stderr.strip()}"
        )
        return False

    umount_result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [host, f"umount {REPLAY_MOUNT_POINT}"],
        capture_output=True,
        text=True,
    )

    if umount_result.returncode != 0:
        logger.error(
            f"{host}: replayed the log on {device} but could not unmount "
            f"{REPLAY_MOUNT_POINT} — {umount_result.stderr.strip()}"
        )
        return False

    logger.info(f"{host}: replayed the filesystem log on {device}")
    return True


def change_filesystem_uuid(host: str, device: str):
    """SSH to host and generate a new filesystem UUID on `device`
    (a full /dev/mapper/<name> path). Supports XFS and ext2/3/4;
    the filesystem must be unmounted.

    xfs_admin exits 0 even when it refuses the change because the log needs
    replaying, so the UUID is read back and compared rather than trusting the
    exit code. If it did not change, the log is replayed by mounting and
    unmounting at /tmp/mount and the change is attempted once more.

    Returns the new UUID (lowercase), or False if the device's filesystem
    type is unsupported, the log could not be replayed, or the UUID did not
    change.
    """
    fstype_result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [host, f"blkid -s TYPE -o value {device}"],
        capture_output=True,
        text=True,
    )
    fstype = fstype_result.stdout.strip().lower()

    if fstype == "xfs":
        cmd = f"xfs_admin -U generate {device}"
    elif fstype in ("ext2", "ext3", "ext4"):
        cmd = f"tune2fs -U random {device}"
    else:
        logger.warning(f"{host}: unsupported or undetected filesystem type '{fstype}' on {device}")
        return False

    original_uuid = filesystem_uuid(host, device)

    def _attempt():
        result = subprocess.run(
            ["ssh"] + _SSH_OPTS + [host, cmd],
            capture_output=True,
            text=True,
        )
        ### xfs_admin reports a refusal on stdout and still exits 0, so the
        ### exit code alone cannot be trusted here
        if result.returncode != 0:
            logger.warning(f"{host}: UUID change failed on {device} — {result.stderr.strip()}")

        changed = filesystem_uuid(host, device)
        if changed and changed != original_uuid:
            return changed
        return False

    new_uuid = _attempt()
    if new_uuid:
        return new_uuid

    logger.info(
        f"{host}: UUID on {device} unchanged, replaying the filesystem log and retrying"
    )
    if not replay_filesystem_log(host, device, fstype):
        return False

    new_uuid = _attempt()
    if not new_uuid:
        logger.error(f"{host}: UUID on {device} still unchanged after replaying the log")
        return False

    return new_uuid


# ---------------- PARTITION LOOKUP ----------------

def highest_partition(host: str, device: str):
    """SSH to host and return the full path of the highest-numbered partition
    on `device` (a full device path, e.g. /dev/mapper/<name> or /dev/vdX),
    via `lsblk -l` (flat output, no tree-drawing prefixes to strip).

    Returns the partition's full path (same directory as `device`), or False
    if the device has no partitions.
    """
    result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [host, f"lsblk -l -no NAME,TYPE {device}"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.warning(f"{host}: lsblk failed on {device} — {result.stderr.strip()}")
        return False

    device_dir, base_name = device.rsplit("/", 1)
    numbers = []
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 2 or columns[1] != "part":
            continue
        name = columns[0]
        suffix = name[len(base_name):] if name.startswith(base_name) else ""
        if suffix.isdigit():
            numbers.append(int(suffix))

    if not numbers:
        return False

    return f"{device_dir}/{base_name}{max(numbers)}"


# ---------------- MOUNT POINT -> DEVICE LOOKUP ----------------

def find_scsi_disk_from_filesystem(host: str, mount_point: str):
    """SSH to host and return the block device backing the filesystem
    mounted at `mount_point`, via findmnt.

    Returns the device path (e.g. /dev/mapper/mpathl1 or /dev/vdg1), or
    False if `mount_point` isn't currently mounted.
    """
    result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [host, f"findmnt --noheadings --output SOURCE --target {mount_point}"],
        capture_output=True,
        text=True,
    )

    device = result.stdout.strip()

    if result.returncode != 0 or not device:
        logger.warning(f"{host}: no device found for mount point {mount_point}")
        return False

    return device


# ---------------- HYPERVISOR-SIDE DEVICE LOOKUP ----------------

def find_scsi_disk_from_domain_device(hypervisor_host: str, domain: str, device: str):
    """SSH to a KVM hypervisor and return the host-side source path backing
    `device` (e.g. 'vdg') on the given `domain`, via `virsh domblklist`.

    Returns the source path (e.g. /dev/mapper/<name> or
    /dev/disk/by-id/dm-uuid-mpath-<wwid>), or False if `device` isn't found
    on that domain (or has no backing source).
    """
    result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [hypervisor_host, f"virsh domblklist {domain}"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.warning(
            f"{hypervisor_host}: virsh domblklist failed for domain {domain} — {result.stderr.strip()}"
        )
        return False

    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 2 or columns[0] != device:
            continue
        return columns[1] if columns[1] != "-" else False

    return False


def filesystem_uuid(host: str, device: str):
    """SSH to host and return the filesystem UUID on `device` (lowercase).

    Returns False when the device has no readable filesystem UUID, which is
    also the case for a whole disk whose filesystem lives on a partition.
    """
    result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [host, f"blkid -s UUID -o value {device}"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.warning(f"{host}: could not read filesystem UUID on {device} — {result.stderr.strip()}")
        return False

    uuid = result.stdout.strip().lower()
    if not uuid:
        logger.warning(f"{host}: no filesystem UUID reported on {device}")
        return False

    return uuid


# ---------------- HYPERVISOR GUEST ATTACH ----------------

def _domain_targets(hypervisor_host: str, domain: str):
    """Return the set of target device names already attached to `domain`."""
    result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [hypervisor_host, f"virsh domblklist {domain}"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.warning(
            f"{hypervisor_host}: virsh domblklist failed for domain {domain} — {result.stderr.strip()}"
        )
        return None

    targets = set()
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 2 or columns[0] in ("Target", "-"):
            continue
        targets.add(columns[0])

    return targets


def next_free_domain_target(hypervisor_host: str, domain: str, prefix: str = "vd"):
    """Return the next unused virtio target name on `domain` (e.g. 'vdg').

    Returns False when the domain's current targets cannot be read.
    """
    targets = _domain_targets(hypervisor_host, domain)
    if targets is None:
        return False

    for letter in "bcdefghijklmnopqrstuvwxyz":
        candidate = prefix + letter
        if candidate not in targets:
            return candidate

    logger.warning(f"{hypervisor_host}: no free {prefix}X target left on domain {domain}")
    return False


def attach_disk_to_domain(hypervisor_host: str, domain: str, device: str):
    """Attach `device` to `domain` on a KVM hypervisor via virsh attach-disk.

    `device` must be the whole disk (e.g. /dev/mapper/mpathl), not a partition.
    The target name is the domain's next free virtio slot, and the disk is
    attached with '--cache none --io native', since the default io=threads
    caches too aggressively for this workload.

    The attach is live only, so it does not persist across a guest reboot.

    Returns the guest device path the disk appears as (e.g. '/dev/vdg'), or
    False if the target could not be chosen or the attach failed.
    """
    target = next_free_domain_target(hypervisor_host, domain)
    if not target:
        return False

    result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [
            hypervisor_host,
            f"virsh attach-disk {domain} {device} {target} --cache none --io native --live --persistent",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.warning(
            f"{hypervisor_host}: attach-disk {device} -> {domain}:{target} failed — "
            f"{result.stderr.strip()}"
        )
        return False

    logger.info(f"{hypervisor_host}: attached {device} to {domain} as {target}")
    return f"/dev/{target}"


# ---------------- HYPERVISOR GUEST LOOKUP ----------------

def find_domain_on_hypervisor(hypervisor_host: str, domain: str):
    """SSH to a KVM hypervisor and look for `domain` in `virsh list --all`.

    Returns the domain's state as reported by virsh (e.g. 'running',
    'shut off'), or False when the hypervisor is unreachable, virsh fails, or
    no domain of that name is defined there.
    """
    result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [hypervisor_host, "virsh list --all"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.warning(
            f"{hypervisor_host}: virsh list failed — {result.stderr.strip()}"
        )
        return False

    ### Id Name State, where Id is '-' for a domain that is not running and
    ### the state itself can be two words ('shut off')
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 3 or columns[0] == "Id":
            continue
        if columns[1] == domain:
            return " ".join(columns[2:])

    logger.warning(f"{hypervisor_host}: no domain named {domain} is defined")
    return False


# ---------------- SCSI UUID LOOKUP ----------------

def find_scsi_uuid(host: str, device: str):
    """SSH to host and return the SCSI UUID for `device`, which may be a
    /dev/mapper multipath device (whole-disk or a partition of one) or a
    raw /dev/sdX device.

    Reads DM_UUID (multipath), falling back to ID_SERIAL_SHORT then ID_WWN,
    via udevadm. A partitioned multipath device's DM_UUID carries a
    'partN-' prefix (e.g. 'part1-mpath-<wwid>') ahead of the 'mpath-'
    prefix; both are stripped, then the leading '3' NAA type digit is
    stripped if present, and the result is returned in lowercase.

    Returns False if no UUID could be determined.
    """
    cmd = (
        f'udevadm info --query=property --name="{device}" 2>/dev/null'
        " | awk -F= '/^DM_UUID/{print $2; exit} /^ID_SERIAL_SHORT/{print $2; exit} /^ID_WWN/{print $2; exit}'"
    )
    result = subprocess.run(
        ["ssh"] + _SSH_OPTS + [host, cmd],
        capture_output=True,
        text=True,
    )

    uuid = result.stdout.strip()

    if result.returncode != 0 or not uuid:
        logger.warning(f"{host}: no SCSI UUID found for {device}")
        return False

    if uuid.startswith("part"):
        rest = uuid[len("part"):]
        digits = 0
        while digits < len(rest) and rest[digits].isdigit():
            digits += 1
        if digits and rest[digits:digits + 1] == "-":
            uuid = rest[digits + 1:]

    if uuid.startswith("mpath-"):
        uuid = uuid[len("mpath-"):]

    if uuid.startswith("3"):
        uuid = uuid[1:]

    return uuid.lower()
