# Generated-By: Codex / gpt-6.1-sol
"""Fleet display precision, unknown values and concise service labels."""

import copy
import math
from pathlib import Path
import re
import runpy

import pytest

from tui.fleet_format import (api_label, count, display_text, duration, gib, model_label,
                              owner_info, status_label, total_tokens)
from tui.fleet_gpu import (account_gpu, allocation_legend, card_header, detail_lines,
                           render_expanded, service_lines)


def test_anonymous_owner_labels_match_standalone_cli_and_keep_namespaces():
    cli = runpy.run_path(str(Path(__file__).parents[1] / "cli/llm"))
    owners = [{"container": "operator"},
              {"host": True, "host_uid": 1000, "host_user": "operator"},
              {"host": True, "host_uid": 1001, "host_user": "operator"}]
    labels = []
    for owner in owners:
        identity, label = owner_info(owner)
        assert re.fullmatch(r"User [0-9]{9}", label)
        assert cli["fleet_owner"](owner) == (identity, label)
        labels.append(label)
    assert len(set(labels)) == 3
    owners[1]["host_user"] = "renamed"
    assert owner_info(owners[1])[1] == labels[1]
    assert owner_info(owners[1], True) == ("host:uid:1000", "renamed")
    assert owner_info({"host": True, "host_uid": 1000}, True)[1] == "UID 1000"
    assert owner_info({}) == ("unknown", "Unknown")
    assert owner_info({"host": True}) == ("host", "Unknown")


@pytest.mark.parametrize("model, expected", [
    ("/srv/models/gemma-4-31b-it-qat-w4a16-ct", "gemma-4-31b-it-qat-w4a16-ct"),
    ("/srv/models/gemma/", "gemma"), ("google/gemma", "google/gemma"),
])
def test_model_paths_have_basename_labels_without_changing_repo_ids(model, expected):
    cli = runpy.run_path(str(Path(__file__).parents[1] / "cli/llm"))
    assert model_label(model) == cli["fleet_model_label"](model) == expected


@pytest.mark.parametrize("name", ["User", "Work", "google"])
def test_raw_text_projection_preserves_labels_and_model_ids(name):
    cli = runpy.run_path(str(Path(__file__).parents[1] / "cli/llm"))
    owner = {"container": name, "model": name + "/gemma"}
    label = owner_info(owner)[1]
    raw = "%s:42:55 · %s/gemma · %s" % (name, name, label)
    expected = "%s:42:55 · %s/gemma · %s" % (label, name, label)
    assert display_text(raw, [owner]) == expected
    assert cli["fleet_display_text"](raw, [owner]) == expected


@pytest.mark.parametrize("name, model", [("gemma-owner", "gemma"),
                                        ("User", "/srv/models/User"),
                                        ("Work", "/srv/models/Work")])
def test_model_tokens_do_not_protect_owner_id_prefixes(name, model):
    cli = runpy.run_path(str(Path(__file__).parents[1] / "cli/llm"))
    owner = {"container": name, "model": model, "id": name + ":42:55"}
    label = owner_info(owner)[1]
    assert display_text(owner["id"], [owner]) == label + ":42:55"
    assert cli["fleet_display_text"](owner["id"], [owner]) == label + ":42:55"
    first, ident, _, _ = service_lines(owner, 1, 0)
    assert "Model " + model_label(model) in first.plain
    assert "Service ID: " + label + ":42:55" in ident.plain
    owner["id"] = name
    assert display_text(name, [owner], preserve_models=False) == label
    assert cli["fleet_display_text"](name, [owner], preserve_models=False) == label
    assert "Service ID: " + label in service_lines(owner, 1, 0)[1].plain


@pytest.mark.parametrize("seconds, expected", [
    (0, "0s"), (.25, "<1s"), (30, "30s"), (59.9, "59s"), (60, "1m"),
    (61.5, "1m 1s"), (3600, "1h"), (3661, "1h 1m"), (86400, "1d"),
    (86461, "1d 1m"), (30 * 86400, "1mo"), (31 * 86400, "1mo 1d"),
    (365 * 86400, "1y"), (395 * 86400, "1y 1mo"),
])
def test_duration_uses_two_integer_units(seconds, expected):
    assert duration(seconds) == expected


@pytest.mark.parametrize("value", [None, -1, float("nan"), float("inf"), True, "12"])
def test_missing_or_invalid_observations_stay_unknown(value):
    assert duration(value) == gib(value) == count(value) == "?"


@pytest.mark.parametrize("value, expected", [
    (0, "0"), (-0.0, "0"), (20.0, "20"), (140.1201171875, "140.12"),
    (.0009765625, "<0.01"), (.009, "<0.01"), (.01, "0.01"), (1.999, "2"),
])
def test_gib_limits_display_precision_without_hiding_tiny_allocations(value, expected):
    assert gib(value) == expected


@pytest.mark.parametrize("value, expected", [
    (0, "0"), (24.0, "24"), (1200, "1,200"), (24000, "24,000"),
    (1200000, "1,200,000"), (1200.75, "1,200"),
])
def test_counts_use_grouped_whole_numbers(value, expected):
    assert count(value) == expected


@pytest.mark.parametrize("prompt, generated, expected", [
    (0, 0, 0), (24000, 12000, 36000), (None, 12000, None),
    (24000, None, None), (False, 0, None), (0, float("nan"), None),
    (1e308, 1e308, None),
])
def test_total_tokens_requires_both_input_and_output(prompt, generated, expected):
    window = {"prompt_tokens": prompt, "gen_tokens": generated, "total_tokens": 999}
    assert total_tokens(window) == expected


@pytest.mark.parametrize("status, expected", [
    ("active", "Active"), ("idle", "Idle"), ("over_limit", "Running · inactive"),
    ("claimed", "Claimed"), ("unknown", "Unknown"), ("unrecognized", "Unknown"),
])
def test_status_labels_keep_machine_values_separate(status, expected):
    assert status_label(status) == expected


@pytest.mark.parametrize("access, address, expected", [
    ("local_only", "http://127.0.0.1:11434", "Local only"),
    ("shared", "http://0.0.0.0:11434", "http://0.0.0.0:11434 · Shared"),
    ("shared", "http://[::]:11434", "http://[::]:11434 · Shared"),
    ("shared", None, "Unknown · Shared"),
    ("direct", "http://192.0.2.1:8000", "http://192.0.2.1:8000"),
    ("direct", None, "Unknown"), ("unknown", None, "Unknown"),
    ("unknown", "http://192.0.2.1:8000", "http://192.0.2.1:8000 · Unknown"),
    (None, None, "Unknown"),
])
def test_api_label_describes_metadata_without_asserting_reachability(access, address, expected):
    assert api_label({"api_access": access, "api_address": address}) == expected


@pytest.mark.parametrize("access", ["shared", "local_only", "direct", "unknown"])
def test_stale_api_metadata_is_unknown_regardless_of_access(access):
    service = {"api_access": access, "api_address": "http://192.0.2.1:8000"}
    original = copy.deepcopy(service)
    assert api_label(service, fresh=False) == "Unknown"
    assert service == original


def test_service_rows_use_durations_token_units_and_api_metadata():
    service = {"id": "example", "model": "Example model", "engine": "vllm",
               "status": "over_limit", "api_access": "shared",
               "api_address": "http://0.0.0.0:8000", "window_24h": {
                   "active_minutes": .5, "coverage_ratio": .5, "requests": 1200,
                   "prompt_tokens": 24000, "gen_tokens": 12000}}
    original = copy.deepcopy(service)
    first, ident, activity, counters = service_lines(service, 52.0009765625, 0)
    assert "State Running · inactive" in first.plain
    assert "GPU 0 VRAM: 52 GiB" in first.plain
    assert "Service ID: example · API: http://0.0.0.0:8000 · Shared" in ident.plain
    assert " active 30s · coverage 50%" in activity.plain
    assert counters.plain == ("    24h service requests 1,200 · input 24,000 tokens"
                              " · output 12,000 tokens · total 36,000 tokens")
    assert service == original


def test_unknown_counts_and_activity_do_not_become_observed_zeroes():
    service = {"id": "example", "window_24h": {"gen_tokens": 4}}
    _, ident, activity, counters = service_lines(service, None, 0)
    assert "API: Unknown" in ident.plain
    assert " active ? · coverage ?" in activity.plain
    assert "requests ? · input ? tokens · output 4 tokens · total ? tokens" in counters.plain


def test_all_gpu_labels_share_precision_while_accounting_keeps_source_values():
    gpu = {"index": 0, "total_gb": 140.1201171875, "used_gb": 100.03125,
           "occupants": [{"container": "tiny", "kind": "other", "used_gb": .0001},
                         {"container": "example", "kind": "llm", "used_gb": 52.0009765625}]}
    original = copy.deepcopy(gpu)
    card = account_gpu(gpu)
    labels = [card_header(card, 200).plain, allocation_legend(card, 200)[0].plain,
              detail_lines(card), render_expanded([card], [], 100)[0].plain]
    assert card.total_gb == gpu["total_gb"]
    assert card.used_gb == gpu["used_gb"]
    assert card.attributed_gb == math.fsum([52.0009765625, .0001])
    assert next(item.used_gb for item in card.allocations if item.identity == "container:tiny") == .0001
    assert gpu == original
    for label in labels:
        assert "140.1201171875" not in label
        assert "100.03125" not in label
        assert "52.0009765625" not in label
    assert all("<0.01" in label for label in labels[1:])


@pytest.mark.parametrize("width", [59, 79])
def test_wrapped_api_metadata_keeps_the_service_hit_and_allocation_anchor(width):
    service = {"id": "example-" + "identity-" * 9, "model": "Example model",
               "api_address": "http://[2001:db8:0:0:0:0:0:1]:12345", "api_access": "shared"}
    card = account_gpu({"index": 0, "total_gb": 100, "used_gb": 52, "occupants": [
        {"container": "example", "kind": "llm", "used_gb": 52, "service_id": service["id"]}]},
        [service])
    view, hits, anchors, service_hits = render_expanded([card], [service], width)
    key = card.allocations[0].key
    rows = [row for row, ident in service_hits.items() if ident == service["id"]]
    joined = "".join("".join(view.plain.splitlines()[row].split()) for row in rows)
    assert "API:" + service["api_address"] + "·Shared" in joined
    assert all((row, 0, width, 0, key) in hits for row in rows)
    assert anchors[(0, key)] < min(rows)
    assert all(line.cell_len <= width for line in view.split("\n"))


@pytest.mark.parametrize("selected_gpu", [0, 1])
def test_stale_expanded_rows_hide_all_api_metadata_without_changing_observations(selected_gpu):
    services = [{"id": "example-%s" % index, "model": "Example model %s" % index,
                 "status": "active", "api_address": "http://0.0.0.0:%s" % (8000 + index),
                 "api_access": "shared", "window_24h": {
                     "active_minutes": .5, "coverage_ratio": .5, "requests": 1200,
                     "prompt_tokens": 24000, "gen_tokens": 12000}} for index in (0, 1)]
    cards = [account_gpu({"index": index, "total_gb": 100, "used_gb": 52.0009765625,
                         "occupants": [{"container": "example", "kind": "llm",
                                        "used_gb": 52.0009765625, "service_id": service["id"]}]},
                         services) for index, service in enumerate(services)]
    original = copy.deepcopy(services)
    fresh_view = render_expanded(cards, services, 120, selected_gpu=selected_gpu)[0].plain
    stale_view, hits, anchors, service_hits = render_expanded(
        cards, services, 120, selected_gpu=selected_gpu, stale=True)
    assert stale_view.plain.count("API: Unknown") == 2
    assert "http://" not in stale_view.plain and "Shared" not in stale_view.plain
    for index, (card, service) in enumerate(zip(cards, services)):
        assert service["api_address"] + " · Shared" in fresh_view
        fresh_lines = service_lines(service, card.used_gb, index)
        stale_lines = service_lines(service, card.used_gb, index, fresh=False)
        assert all(fresh_lines[row] == stale_lines[row] for row in (0, 2, 3))
        assert card.used_gb == card.allocations[0].used_gb == 52.0009765625
        key = card.allocations[0].key
        rows = [row for row, ident in service_hits.items() if ident == service["id"]]
        assert anchors[(index, key)] < min(rows)
        assert all((row, 0, 120, index, key) in hits for row in rows)
    assert services == original


def test_fresh_unknown_activity_keeps_verified_api_metadata():
    service = {"id": "unsupported-activity", "model": "Example model", "engine": "ollama",
               "status": "unknown", "idle_time_sensitive": False,
               "api_address": "http://0.0.0.0:11434", "api_access": "shared"}
    card = account_gpu({"index": 0, "total_gb": 100, "used_gb": 52, "occupants": [
        {"container": "example", "kind": "llm", "used_gb": 52, "service_id": service["id"]}]},
        [service])
    original = copy.deepcopy(service)
    view = render_expanded([card], [service], 120, statuses={service["id"]: "unknown"})[0].plain
    assert "State Unknown" in view
    assert "active ? · coverage ?" in view
    assert "API: http://0.0.0.0:11434 · Shared" in view
    assert "input ? tokens · output ? tokens · total ? tokens" in view
    assert service == original
