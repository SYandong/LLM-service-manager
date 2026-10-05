#!/usr/bin/env bash
# Generated-By: Codex / gpt-6.1-sol
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -I -B "$script_dir/fleet-scan-admin.py" install "$@"
