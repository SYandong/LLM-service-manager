# Generated-By: Codex / gpt-6-astra
"""File registration alone must not confer runtime model/action authority."""

from dataclasses import replace

import pytest

from llmsvc.actions import ManagedModelTransport
from llmsvc.leases import PlacementController
from llmsvc.state import Pin
from llmsvc.store import IntentStore
from test_registry_http_preview import mounted, registry_fixture, request


@pytest.mark.parametrize("pinned", [False, True])
def test_registered_file_model_without_trusted_runtime_profile_cannot_place(mounted, tmp_path, pinned):
    scheduler = mounted.scheduler
    database = tmp_path / "runtime-catalog.sqlite"
    config = replace(scheduler.config, read_only=False, placement_enabled=True,
        state_db_path=str(database), collectors={"models": {"base": {"unit": "vllm-base.service", "util": 0.3, "weights_gb": 10}}})
    scheduler.config = config
    store = IntentStore(database, action_lock=scheduler.action_lock)
    scheduler.store = store
    def forbidden(*args, **kwargs):
        pytest.fail("Unconfigured runtime model reached a probe or actuator")
    transport = ManagedModelTransport(swap_url="http://127.0.0.1:1", models=config.collectors["models"],
                                      systemctl="unused-test-systemctl", run=forbidden)
    scheduler.placement = PlacementController(scheduler, transport, probe=forbidden)
    try:
        if pinned:
            store.put_pin(Pin("saved", mounted.clock[0]+3600, "fixture-owner"))
        status, listed = request(mounted.address, "GET", "/v1/models")
        assert status == 200 and "saved" in listed["records"]
        assert listed["records"]["saved"]["base"] == "base"
        assert "saved" not in transport.models and "saved" not in transport.units
        before = mounted.files(), scheduler.events_since(0), store.active(mounted.clock[0])
        status, result = request(mounted.address, "POST", "/v1/place", {"model": "saved", "util": 0.3})
        assert status == 404 and result["error"] == "unknown_model"
        assert not store.leases()
        assert (mounted.files(), scheduler.events_since(0), store.active(mounted.clock[0])) == before
        if pinned:
            assert store.active(mounted.clock[0])[0][0].model == "saved"
    finally:
        store.close()
