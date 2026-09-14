# Generated-By: Claude Code / claude-fable-5-1
"""Import through the real registry/catalog lifecycle with a generated profile."""

from dataclasses import replace

import pytest
import yaml

from llmsvc.__main__ import build_profile_provider
from llmsvc.discovery import ModelDiscovery
from llmsvc.registry import ModelRegistry
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


def submit(c, body):
    status, job = request(c.address, "POST", "/v1/models", body)
    return status, job


def test_import_installs_a_generated_profile_and_collector_entry(imported):
    c, api, weights, _ = imported
    status, job = submit(c, {"import": "candidate"})
    assert status == 200, job
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
    assert [row["status"] for row in listed["discovered"]] == ["imported"]


def test_minimal_descriptor_takes_util_from_base_and_measures_weights(imported):
    c, api, weights, _ = imported
    describe(weights, {"base": "base"})
    status, job = submit(c, {"import": "candidate"})
    assert status == 200, job
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
    status, result = submit(c, {"import": "candidate"})
    assert status == 400 and "GPU total memory is unknown" in result["message"]
    assert api.records() == {} and c.path.read_bytes() == before


def test_configured_profile_must_agree_with_the_imported_descriptor(imported):
    c, api, weights, configured = imported
    c.runtime.profile_provider = build_profile_provider(replace(configured, catalog_profiles={
        "base": profile("base", 21000), "candidate": profile("candidate", 21009)}), c.s)
    before = c.path.read_bytes()
    status, result = submit(c, {"import": "candidate"})
    assert status == 400 and "disagrees on" in result["message"]
    assert api.records() == {} and c.path.read_bytes() == before


def test_model_without_profile_or_descriptor_still_needs_one(imported):
    c, api, weights, configured = imported
    c.runtime.profile_provider = build_profile_provider(replace(configured, catalog_profiles={}), c.s)
    status, result = submit(c, {"name": "plain", "path": str(weights), "base": "base"})
    assert status == 400 and "lacks a trusted maintenance profile" in result["message"]
