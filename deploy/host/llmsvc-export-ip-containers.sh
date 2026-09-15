#!/usr/bin/env bash
# Generated-By: OpenCode / deepseek-v4.1-flash
#
# Export the host's LXD container IP -> container-name map for the scheduler's
# usage attribution. The patched llama-swap records the request peer address as
# client_ip in activity.metadata_json; this export lets the scheduler label a
# container's IP with its name.
#
# Usage: llmsvc-export-ip-containers.sh [output-directory]
set -euo pipefail

dest_dir="${1:-/var/lib/llmsvc-host-export}"
mkdir -p "$dest_dir"

dest="$dest_dir/ip-containers.json"
tmp="$dest_dir/.ip-containers.json.tmp"

# Map only globally routable IPv4 addresses; loopback/link-local addresses are
# never a container's peer identity. The JSON work is done with python3 because
# jq is not guaranteed on the host.
lxc list --format json | python3 -c '
import json
import sys
import time

try:
    containers = json.load(sys.stdin)
except Exception:
    raise SystemExit("invalid lxc list JSON")

if not isinstance(containers, list):
    raise SystemExit("lxc list did not return a list")

mapping = {}
for container in containers:
    if not isinstance(container, dict):
        continue
    name = container.get("name")
    if not isinstance(name, str) or not name:
        continue
    state = container.get("state")
    network = state.get("network") if isinstance(state, dict) else None
    if not isinstance(network, dict):
        continue
    for interface in network.values():
        if not isinstance(interface, dict):
            continue
        addresses = interface.get("addresses")
        if not isinstance(addresses, list):
            continue
        for address in addresses:
            if not isinstance(address, dict):
                continue
            if address.get("family") != "inet" or address.get("scope") != "global":
                continue
            value = address.get("address")
            if isinstance(value, str) and value:
                mapping[value] = name

json.dump({"generated_at": int(time.time()), "containers": mapping}, sys.stdout)
' > "$tmp"

chmod 0644 "$tmp"
mv -f "$tmp" "$dest"
