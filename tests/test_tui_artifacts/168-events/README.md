# #168 event readability and export evidence

Actual interactive Textual PTY captures using synthetic scheduler SSE records. They do not contain the user's private screenshot or production data. Each run sends123 records, including20 repeated timeout/disconnect/connect/unknown-inflight/unchanged-state cycles, local allowlist drops, and a final real state change within the fixture. The UI retains123 raw records and renders12 wrapped lines. Counters show20 errors and20 local drops; in-flight remains unknown. Full raw/provenance data is available in the frozen details view.

- `wide-screen.png` / `.svg`:100×30 main view; `wide-details.*`: actual detail window.
- `narrow-screen.*` / `narrow-details.*`:40×24 equivalents.
- `*-session.cast`: actual asciicastv2 output, including opening and closing details. No Copy or Save action is sent by the recorder.
- `*-metrics.json`: short-run frame/input/selection/cleanup measurements and runtime hashes. These are2-second UI smoke windows, NOT new60-second CPU acceptance or #185 transport evidence. Key-to-output samples are15.36–18.32ms wide and14.66–28.26ms narrow;0table rebuilds, stable selection and reader cleanup passed.

The recordings were captured before the final export-path Enter isolation follow-up; that follow-up changes only Input submission guards, not layout/rendering, and has separate current/minimum UI regression coverage. Recorded runtime hashes are retained rather than relabelled as the follow-up.

The original SVGs are Textual exports; PNGs are rendered by installed librsvg/cairo. Rich adds the screenshot title/window chrome; the application does not restore a Header. Filesystem/clipboard product behavior is covered by actual UI tests on Textual0.70.0 and8.2.8: Shift+arrow selection, explicit OSC52 request, no-selection compact copy with200records whose raw export exceeds64KiB, oversized-selection fallback, fullUTF8 save0600, existing-file/symlink refusal and close-during-save guards. The real laptop clipboard is not acknowledged or claimed; the message is a request, with Save text as fallback.

Reproduce in an installed Textual environment:

```sh
python tests/tui_benchmark.py --seconds 2 --event-storm --details --output /tmp/llm-events-wide
python tests/tui_benchmark.py --seconds 2 --width 40 --height 24 --event-storm --details --output /tmp/llm-events-narrow
```

Output directories must not exist. All HTTP/PTY resources belong to the synthetic fixture and are cleaned up. Long-term behavior and remote terminal display latency remain unmeasured. The earlier60-second #168 artifacts are retained separately and were not rerun for this product slice.

<!-- Generated-By: Codex / gpt-6-astra -->
