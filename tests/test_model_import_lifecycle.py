# Generated-By: Claude Code / claude-fable-5-1
"""Import through the real registry/catalog lifecycle with a generated profile."""

from dataclasses import replace

import pytest
import yaml

from llmsvc.__main__ import build_profile_provider
from llmsvc.discovery import ModelDiscovery
from llmsvc.registry import ModelRegistry, RegistryError
from test_catalog_lifecycle import catalog, profile
from test_model_import_http import describe
from test_registry_api import registry
from test_registry_http_preview import request
from test_reload import make_quiet


@pytest.fixture
def imported(catalog, registry):
    c = catalog
    _, source, weights, *_ = registry
    document = yaml.safe_load(source.path.read_bytes())
    document["models"]["base"]["macros"]["util"] = ".4"
    c.path.write_bytes(yaml.safe_dump(document).encode().replace(b"8101", b"21000"))
    describe(weights, {"base": "base", "util": .4, "weights_gb": 10})
    configured = replace(c.cfg, catalog_profiles={"base": profile("base", 21000)})
    c.runtime.profile_provider = build_profile_provider(configured, c.s)
    c.runtime.instance_provider = lambda **kw: c.binding.instance
    api = ModelRegistry(c.q, shared_roots=(weights.parent,), daemon_port_range=(21000, 21010),
        discover=ModelDiscovery([weights.parent]), now=c.clock,
        stop_model=lambda *a, **kw: pytest.fail("import must not stop a model"),
        unit_absent=lambda name, **kw: True)
    c.runtime.connect_registry(api)
    c.s.registry = api
    return c, api, weights, configured


def submit(api, body):
    return api.add(body)


def test_import_installs_a_generated_profile_and_collector_entry(imported):
    c, api, weights, _ = imported
    job = submit(api, {"import": "candidate"})
    assert job["description"] == {"kind": "add_model", "model": "candidate", "base": "base"}
    make_quiet(c.q.quiet, c.clock)
    assert c.runtime.process_once()["status"] == "applied"
    c.s.sample_once()
    record = api.records()["candidate"]
    assert record["util"] == .4 and record["weights_gb"] == 10 and record["daemon_port"] == 21001
    # budget_gb is the configured share of the observed 100 GiB fixture card.
    assert c.s.placement.transport.models["candidate"] == {
        "unit": "vllm-candidate.service", "daemon_url": "http://127.0.0.1:21001", "port": 21001,
        "util": .4, "weights_gb": 10.0, "budget_gb": 40.0, "is_default": False}
    status, listed = request(c.address, "GET", "/v1/models")
    assert status == 200
    assert [row["status"] for row in listed["discovered"]] == ["configured"]


def test_minimal_descriptor_takes_util_from_base_and_measures_weights(imported):
    c, api, weights, _ = imported
    describe(weights, {"base": "base"})
    submit(api, {"import": "candidate"})
    make_quiet(c.q.quiet, c.clock)
    assert c.runtime.process_once()["status"] == "applied"
    c.s.sample_once()
    installed = c.s.placement.transport.models["candidate"]
    assert installed["util"] == .4 and installed["budget_gb"] == 40.0
    assert 0 < installed["weights_gb"] < 1  # Measured from the fixture's tiny weights.


def test_import_blocks_when_no_card_size_is_observed(imported, monkeypatch):
    c, api, weights, _ = imported
    observed = c.s.snapshot
    monkeypatch.setattr(c.s, "snapshot", lambda: replace(observed(), gpus=()))
    before = c.path.read_bytes()
    with pytest.raises(RegistryError, match="GPU total memory is unknown"):
        submit(api, {"import": "candidate"})
    assert api.records() == {} and c.path.read_bytes() == before


def test_configured_profile_must_agree_with_the_imported_descriptor(imported):
    c, api, weights, configured = imported
    c.runtime.profile_provider = build_profile_provider(replace(configured, catalog_profiles={
        "base": profile("base", 21000), "candidate": profile("candidate", 21009)}), c.s)
    before = c.path.read_bytes()
    with pytest.raises(RegistryError, match="disagrees on"):
        submit(api, {"import": "candidate"})
    assert api.records() == {} and c.path.read_bytes() == before


def test_model_without_profile_or_descriptor_still_needs_one(imported):
    c, api, weights, configured = imported
    c.runtime.profile_provider = build_profile_provider(replace(configured, catalog_profiles={}), c.s)
    with pytest.raises(RegistryError, match="lacks a trusted maintenance profile"):
        submit(api, {"name": "plain", "path": str(weights), "base": "base"})


def test_directory_reconciler_drives_the_real_scheduler_registry_and_catalog(imported):
    """Real objects, no fakes: the reconciler's preconditions must match the actual
    shapes (boolean catalog_pending, bound submit_change, wall-clock snapshot age)."""
    from llmsvc.reconcile import DirectoryReconciler
    c, api, weights, _ = imported
    reconciler = DirectoryReconciler(c.s, api, interval_seconds=30, clock=c.s.monotonic)
    c.s.reconciler = reconciler  # GET /v1/models annotates through the mounted reconciler
    result = reconciler.run_once()
    assert result == {"action": "add", "model": "candidate", "reason": None}, result
    make_quiet(c.q.quiet, c.clock)
    assert c.runtime.process_once()["status"] == "applied"
    c.s.sample_once()
    assert "candidate" in api.records()
    status, listed = request(c.address, "GET", "/v1/models")
    assert status == 200 and [row["status"] for row in listed["discovered"]] == ["configured"]
    # Descriptor gone: the record is an orphan and, since the fixture observes the
    # model as stopped, the removal is submitted on the next scan.
    (weights / "llmsvc.json").unlink()
    reconciler._last_submission = reconciler._last_scan = None
    reconciler._backoff.clear()  # the post-add backoff would otherwise defer the orphan for 60 s
    result = reconciler.run_once()
    assert result["model"] == "candidate" and result["action"] in ("remove", None), result
    if result["action"] is None:
        # A stale or unknown fixture observation is the only acceptable reason to wait.
        assert result["reason"] in ("snapshot_blocked", "unknown_model_state"), result
    status, listed = request(c.address, "GET", "/v1/models")
    assert status == 200 and [row["status"] for row in listed["discovered"]] == ["orphaned"]
