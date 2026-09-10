# #168 actual terminal evidence

These are actual interactive Textual screenshots and PTY output from synthetic read-only loopback fixtures, not production measurements or an artist mockup. SVG is the original Textual export; PNG is rasterized with the installed system librsvg/cairo. The window title/chrome belongs to Rich's SVG export, not an application Header.

| Check | 100×30, 60 seconds | 40×24, 2-second smoke |
|---|---:|---:|
| Python / Textual | 3.10.12 / 8.2.8 | same |
| Process CPU, one logical core = 100% | 0.2333% | below 0.01s CPU tick resolution; not 60s evidence |
| Input key → changed text received on PTY | 14.92–28.22ms (10 keys) | 14.50–25.71ms (10 keys) |
| Idle compositor frame max | 1.281ms | 4.926ms |
| Steady table clears | 0 | 0 |
| Selected model retained | yes | yes |
| History / visible lines | 2 / 4 | 2 / 4 |
| Reader closed | yes | yes |

The shared runtime's interpreter/package versions were checked read-only and matched this environment. CPU uses Linux process user+system ticks divided by monotonic elapsed time (not divided by machine CPU count). Latency waits for the actual changed command text in PTY output; it does not count unrelated output as a repaint. Remote network/terminal-emulator display latency and long-term stability are NOT MEASURED. Render timings include compositor refresh and output generation; all raw per-frame samples and runtime source hashes are in JSON.

Reproduce with the installed Textual interpreter:

```sh
python tests/tui_benchmark.py --seconds 60 --output /tmp/llm-tui-new-measurement
python tests/tui_benchmark.py --seconds 2 --width 40 --height 24 --output /tmp/llm-tui-new-narrow
```

Output paths must not exist. The benchmark runs only its own loopback server, uses the real SchedulerClient/EventReader/App, receives comment heartbeats, refreshes state every5s, then types through a real PTY and exits with the normal key binding. No GPU/inference/config/unit/production changes. The100×30 recording is `100x30.cast` (asciicastv2); JSON includes all frame durations, requests, cleanup and selection. Original raw terminal bytes and debug attempts remain in the private coordination artifact directory. Earlier smoke/baseline runs are not substituted for the final60s measurement.

User visual confirmation is pending; screenshot generation alone does not satisfy that acceptance item. Current-head CI and independent Fable approval are separate merge gates; ops/integration own reviewed rollout under current authority.

<!-- Generated-By: Codex / gpt-6-astra -->
