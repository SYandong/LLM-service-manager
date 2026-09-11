#!/bin/sh
# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-5.6-luna
set -eu
exec python3 "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/maintenance_upgrade.py" "$@"
