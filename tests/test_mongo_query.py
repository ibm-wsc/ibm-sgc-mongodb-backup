#!/usr/bin/env python3

# ####################################################################################
#
# Purpose : Parent test utility for hosts/mongo_query.py. Exercises the query
#           functions in that module against a live Ops Manager, one function
#           per --check. As mongo_query.py grows new functions, add a matching
#           --check here rather than writing a new script.
#
# Usage   : test_mongo_query.py [-h]
#                                --cluster-name CLUSTER_NAME
#                                [--check list-nodes]
#                                [--base-url OPSMANAGER_URL]
#                                [--group-id GROUP_ID]
#                                [--public-key PUBLIC_KEY]
#                                [--private-key PRIVATE_KEY]
#
# ####################################################################################

import argparse
import json
import os
import sys
from requests.auth import HTTPDigestAuth

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hosts import mongo_query

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

# ---------------- Checks ----------------

def check_list_nodes(args, auth):
    nodes = mongo_query.list_cluster_nodes(args.base_url, args.group_id, auth, args.cluster_name)
    print(f"\n{len(nodes)} node(s) in cluster '{args.cluster_name}':\n")
    for n in nodes:
        print(f"  ReplicaSet : {n['rs_id']}")
        print(f"  Node       : {n['id']}")
        print(f"  dbPath     : {n['dbPath']}")
        print(f"  State      : {n['memberState']}")
        print()
    return nodes

CHECKS = {
    "list-nodes": check_list_nodes,
}

# ---------------- MAIN ----------------

def main():
    parser = argparse.ArgumentParser(description="Test utility for hosts/mongo_query.py")
    parser.add_argument("--cluster-name", required=True)
    parser.add_argument("--check", choices=sorted(CHECKS), default="list-nodes")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--group-id", default=None)
    parser.add_argument("--public-key", default=None)
    parser.add_argument("--private-key", default=None)
    parser.add_argument("--json", action="store_true", help="Print raw result as JSON instead")
    args = parser.parse_args()
    resolve_credentials(args)

    auth = HTTPDigestAuth(args.public_key, args.private_key)

    result = CHECKS[args.check](args, auth)

    if args.json:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
