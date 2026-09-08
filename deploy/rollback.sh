#!/bin/sh
# Generated-By: Codex / gpt-6-astra
set -eu
exec python3 "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/manage.py" rollback "$@"
