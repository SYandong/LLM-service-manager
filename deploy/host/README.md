# Host IP → container export

<!-- Generated-By: OpenCode / deepseek-v4.1-flash -->

The patched llama-swap writes the request peer address as `client_ip` in
`activity.metadata_json`. The scheduler's usage report maps those addresses to
container names with a small JSON file that this host-side export regenerates
every couple of minutes:

```json
{"generated_at": 1700000000, "containers": {"10.86.229.182": "llmsvc"}}
```

Keeping the export on the host (and the map read-only inside the container)
means the container never needs access to the LXD socket.

## Install the timer

Copy the script and units to the host, then enable the timer:

```bash
sudo install -d /usr/local/lib/llmsvc
sudo install -m 0755 llmsvc-export-ip-containers.sh /usr/local/lib/llmsvc/
sudo install -m 0644 llmsvc-export-ip-containers.service /etc/systemd/system/
sudo install -m 0644 llmsvc-export-ip-containers.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llmsvc-export-ip-containers.timer
```

The script writes `/var/lib/llmsvc-host-export/ip-containers.json` atomically
(a same-directory `.tmp` file, then `mv -f`). The export directory must exist on
the host before the first run.

## Expose the directory to the container

Mount the **directory**, not the file: the atomic rename replaces the inode, so
a file bind would pin the container to a stale copy.

```bash
lxc config device add llmsvc llmsvc-host-export disk \
  source=/var/lib/llmsvc-host-export \
  path=/var/lib/llmsvc-host/export \
  readonly=true
```

## Point the scheduler at it

```yaml
collectors:
  ip_containers_path: /var/lib/llmsvc-host/export/ip-containers.json
```

Optional companions in the same `collectors` block:

```yaml
  # Peers that mean "the host machine"; default is [127.0.0.1, ::1].
  host_ips: ["127.0.0.1", "::1"]
  # Timezone for by=day bucketing; default UTC.
  usage_timezone: UTC
```

A missing, unreadable or invalid export is treated as an empty map: the report
stays available and its `attribution.map_source` says `config`, `file`,
`config+file` or `none`. Static `ip_containers` entries win over the file for
the same IP.
