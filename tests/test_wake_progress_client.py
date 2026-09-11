# Generated-By: Codex / gpt-5.6-luna
"""CLI/TUI consume the sanitized scheduler wake-progress envelope only."""

import runpy
from pathlib import Path


def api():
    return runpy.run_path(str(Path(__file__).parents[1] / "cli" / "llm"))


def event(detail, *, model="model", event_id=8, timestamp=100.0):
    return {"id": event_id, "timestamp": timestamp, "kind": "wake_progress", "model": model,
            "detail": detail}


def detail(stage="health_wait", **changes):
    value = {"stage": stage, "source": "llama-swap", "source_model": "model",
             "progress_source": "per_model_log", "log_epoch": "abc", "sequence": 2,
             "received_at": 100.0, "trusted_for_quiet": False}
    value.update(changes)
    return value


def test_cli_progress_parser_rejects_stale_wrong_source_and_untrusted_shapes():
    module = api()
    assert module["parse_wake_progress"](event(detail()), "model", after_id=7, since=99)["label"] == "waiting for health"
    assert module["parse_wake_progress"](event(detail(), event_id=7), "model", after_id=7) is None
    assert module["parse_wake_progress"](event(detail(source="proxy")), "model") is None
    assert module["parse_wake_progress"](event(detail(source_model="other")), "model") is None
    assert module["parse_wake_progress"](event(detail(stage="loading_weights")), "model") is None
    assert module["parse_wake_progress"](event(detail(trusted_for_quiet=True)), "model") is None
    assert module["parse_wake_progress"](event(detail(), model="other"), "model") is None


def test_cli_progress_labels_never_include_raw_log_values():
    module = api()
    parsed = module["parse_wake_progress"](event(detail("process_started")), "model")
    assert module["format_wake_progress"](parsed) == "daemon process started"
    assert parsed["log_epoch"] == "abc"
    assert "PID" not in module["format_wake_progress"](parsed)
    assert module["format_wake_progress"]({"stage": "unavailable"}) == "progress unavailable"
