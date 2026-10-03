#!/usr/bin/env python3

# ####################################################################################
#
# Purpose : Shared Ops Manager query helpers, callable from opsman_backup.py and
#           opsman_restore.py (and standalone via tests/test_mongo_query.py).
#
# ####################################################################################

import os

import requests

HEADERS = {"Content-Type": "application/json"}

### Ops Manager credentials are taken from the environment, never read from a
### file by the utilities themselves. Source opsman.cred into your shell first:
###     source opsman.cred
OPSMAN_ENV_VARS = ("OPSMAN_BASE_URL", "OPSMAN_GROUP_ID", "OPSMAN_PUBLIC_KEY", "OPSMAN_PRIVATE_KEY")


def resolve_opsman_credentials(base_url=None, group_id=None, public_key=None, private_key=None):
    """Return (base_url, group_id, public_key, private_key).

    Each value is taken from its argument when one is supplied on the command
    line, and otherwise from the matching OPSMAN_* environment variable.
    Raises SystemExit naming any value that is still missing.
    """
    supplied = (base_url, group_id, public_key, private_key)
    resolved = []
    missing = []

    for value, var in zip(supplied, OPSMAN_ENV_VARS):
        value = value or os.environ.get(var)
        if not value:
            missing.append(var)
        resolved.append(value)

    if missing:
        raise SystemExit(
            "Missing Ops Manager credentials: " + ", ".join(missing) + "\n"
            "Source the credentials into your shell (source opsman.cred), or pass "
            "--base-url/--group-id/--public-key/--private-key on the command line."
        )

    return tuple(resolved)


def _api(method, url, auth, payload=None, timeout=60):
    r = requests.request(method, url, auth=auth, headers=HEADERS, json=payload, timeout=timeout)
    if r.text:
        data = r.json()
        r.raise_for_status()
        return data
    return {}


def find_cluster(base_url, group_id, auth, cluster_name):
    url = f"{base_url}/api/public/v1.0/backup/third_party/group/{group_id}/clusters"
    data = _api("GET", url, auth)
    for c in data.get("clusters", []):
        if c["clusterName"] == cluster_name:
            return c["clusterId"]
    raise RuntimeError(f"Cluster '{cluster_name}' not found")


def list_cluster_nodes(base_url, group_id, auth, cluster_name):
    """Return every node in the named cluster, grouped by replica set."""
    cluster_id = find_cluster(base_url, group_id, auth, cluster_name)
    url = f"{base_url}/api/public/v1.0/backup/third_party/group/{group_id}/clusters/{cluster_id}"
    data = _api("GET", url, auth)

    nodes = []
    for rs in data.get("replicaSets", []):
        rs_id = rs.get("id", "unknown-rs")
        for n in rs.get("nodes", []):
            nodes.append({
                "rs_id": rs_id,
                "id": n["id"],
                "dbPath": n.get("dbPath", ""),
                "memberState": n.get("memberState", ""),
            })
    return nodes
