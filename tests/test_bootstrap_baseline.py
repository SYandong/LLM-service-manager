# Generated-By: Codex / gpt-6-astra
"""Zero-effect unleased-preload baseline and internal launcher progress hooks."""
import time
from types import SimpleNamespace

import pytest

from deploy.maintenance_executor import ExecutorError, ScopeInspector, digest
from test_deploy_maintenance_native import native
from test_deploy_launch import load_launcher, write_config, FakeHTTP, argv


def test_unleased_default_preload_cannot_enter_normal_maintenance(native, monkeypatch):
    adapter, scope, _ = native
    adapter.config.write_text('models: {m: {}}\nhooks: {on_startup: {preload: [m]}}\n')
    source = {'pid':31,'start_ticks':'100','scope_sha256':digest(scope)}
    monkeypatch.setattr(ScopeInspector, 'inspect', lambda *args:{'identity':source,'scope':scope,'actors':[source]})
    adapter.show = lambda *args,**kwargs:{'Restart':'no'}
    adapter.native_image = lambda *args:None
    adapter.listener_owned = lambda *args:True
    adapter.http.snapshot = lambda *args:SimpleNamespace(states={'m':'stopped'},requests={})
    calls = []
    adapter.runner = lambda *args:calls.append(args)
    before = {p:p.read_bytes() for p in adapter.state.parent.rglob('*') if p.is_file()}
    with pytest.raises(ExecutorError, match='active_native_model_unleased'):
        adapter.operation('preflight', {'accounts':[]}, time.monotonic()+5)
    assert not calls
    assert {p:p.read_bytes() for p in adapter.state.parent.rglob('*') if p.is_file()} == before
    assert 'preload' in adapter.config.read_text()  # No default deletion to pass.


def launcher_fixture(tmp_path, monkeypatch):
    launcher = load_launcher()
    config = write_config(tmp_path, startup_timeout_seconds=5)
    http = FakeHTTP([(200,{'gpu':0,'lease_id':'lease-a'}),(200,{'lease_id':'lease-a','status':'confirmed'})])
    monkeypatch.setattr(launcher,'request_json',http)
    monkeypatch.setattr(launcher,'unit_exists',lambda unit:False)
    monkeypatch.setattr(launcher,'unit_runtime_state',lambda unit:'active')
    monkeypatch.setattr(launcher,'health_ok',lambda cfg,port:True)
    return launcher,config,http


def test_launcher_records_lease_and_start_submission_before_effect(tmp_path, monkeypatch):
    launcher,config,http = launcher_fixture(tmp_path,monkeypatch)
    stages = []
    def start(cfg,args,model,placement):
        assert stages[-2:] == ['placed','start_submitted']
        assert placement.lease_id == 'lease-a'
        return True
    monkeypatch.setattr(launcher,'start_unit',start)
    assert launcher.main(argv(config), progress=lambda stage,fields:stages.append(stage), deadline=time.monotonic()+5) == 0
    assert stages == ['placing','placed','start_submitted','start_acknowledged','health_observed','confirm_submitted','confirmed']
    assert http.requests[-1][1].endswith('/lease-a/confirm')
    assert launcher._CALL.get() is None


def test_missing_durable_start_receipt_prevents_launch(tmp_path, monkeypatch):
    launcher,config,http = launcher_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(launcher,'start_unit',lambda *args:pytest.fail('start must follow durable receipt'))
    def record(stage,fields):
        if stage == 'start_submitted': raise OSError('fixture failed durable write')
    assert launcher.main(argv(config),progress=record) == 1
    assert len(http.requests) == 1  # Real implementation retains the placed budget.
    assert launcher._CALL.get() is None


def test_bootstrap_cannot_relabel_preexisting_unit_as_its_success(tmp_path, monkeypatch):
    launcher,config,http = launcher_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(launcher,'unit_exists',lambda unit:True)
    assert launcher.main(argv(config),progress=lambda *args:None) == 75
    assert not http.requests


def test_internal_deadline_expiry_before_start_is_not_renewed(tmp_path, monkeypatch):
    launcher,config,http = launcher_fixture(tmp_path,monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(launcher.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(launcher,'start_unit',lambda *args:pytest.fail('late start'))
    def record(stage,fields):
        if stage == 'placed': clock[0] = 102.0
    assert launcher.main(argv(config),progress=record,deadline=101.0) == 75
    assert len(http.requests) == 1
