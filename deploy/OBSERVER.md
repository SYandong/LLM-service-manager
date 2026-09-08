# Observer Activation Helper

`deploy/observer.py` renders and verifies the observation-only systemd surface
described by `observer-activation-proposal.json`. It is limited to the scheduler
read-only service, the capture oneshot service, the capture timer, their
configuration files, the private observation directory, and the helper's own
manifest. It does not change TTL, reaper, llama-swap routing, model units, GPU
workloads, or production rollback state.

Staging rehearsal:

```sh
mkdir -p /tmp/llmsvc-observer-stage
python3 -m deploy.observer stage \
  --settings deploy/deployment.example.json \
  --proposal deploy/observer-activation-proposal.json \
  --config deploy/scheduler.observation.yaml \
  --root /tmp/llmsvc-observer-stage \
  --dry-run
python3 -m deploy.observer stage \
  --settings deploy/deployment.example.json \
  --proposal deploy/observer-activation-proposal.json \
  --config deploy/scheduler.observation.yaml \
  --root /tmp/llmsvc-observer-stage
python3 -m deploy.observer remove \
  --settings deploy/deployment.example.json \
  --proposal deploy/observer-activation-proposal.json \
  --config deploy/scheduler.observation.yaml \
  --root /tmp/llmsvc-observer-stage
```

The dry-run command validates `read_only: true`, proposal `read_only: true`, the
required `--dry-run` scheduler execution flag, paths, unit names, and capture
settings, then prints a JSON plan without creating files, directories, services,
or subprocesses. The non-dry-run staging command writes rendered units/configs
under the staging root and records SHA-256 ownership in
`/opt/llmsvc-scheduler/observer-manifest.json` inside that root. `remove` refuses
forged directory scope, duplicate manifest roles, changed managed files and nonempty observation directories.

The live commands are intentionally exact and manifest-gated:

```sh
python3 -m deploy.observer stage \
  --settings /opt/llmsvc-source/deploy/deployment.example.json \
  --proposal /opt/llmsvc-source/deploy/observer-activation-proposal.json \
  --config /opt/llmsvc-source/deploy/scheduler.observation.yaml \
  --root / \
  --dry-run
python3 -m deploy.observer start \
  --settings /opt/llmsvc-source/deploy/deployment.example.json \
  --proposal /opt/llmsvc-source/deploy/observer-activation-proposal.json \
  --config /opt/llmsvc-source/deploy/scheduler.observation.yaml \
  --root / \
  --dry-run
```

Final approval, if granted later, should be for the reviewed non-dry-run form of
the same `stage` and `start` commands. `start` checks the manifest, current file
hashes, core-validated `read_only` config, and keyed systemd `FragmentPath` plus
per-installation ownership-token/description checks (no unreviewed drop-ins) before it
runs `systemctl daemon-reload`, `systemctl start llmsvc-scheduler.service`, and
`systemctl start llmsvc-observation-capture.timer`. `stop` and `remove` only
target `llmsvc-observation-capture.timer`,
`llmsvc-observation-capture.service`, and `llmsvc-scheduler.service`; they never
use a glob and never touch `vllm-*` model units.

Residuals before live approval:

- The scheduler package and source checkout must already be installed at the
  paths referenced by the rendered units. Provision only the package venv/source
  first; do not run `install.sh` at the same unit/config destinations as this
  helper, since both render those files. The helper refuses to adopt or overwrite
  another install's unit/config. An existing venv is preserved on observer remove;
  package/backup removal remains the separate installer responsibility.
- The alternate port, unit names, config paths, manifest path, private evidence
  directory, and capture capacity must be checked as unused or owned by this
  rollout.
- A full production rollback for later TTL/reaper/routing writes is still not
  solved by this observer helper. This helper only rolls back the observer by
  stopping/removing its owned read-only scheduler and capture resources.


The timer has `OnActiveSec=1s` for the first capture and `OnUnitActiveSec=15` for subsequent samples. Live start failure stops only units whose runtime ownership token still matches. No live helper action was executed during the staged rehearsal.

<!-- Generated-By: Codex / gpt-6-astra -->
