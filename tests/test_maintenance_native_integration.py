# Generated-By: Codex / gpt-6-astra
"""Core phases + actual native adapter observation dispatch/account predicates.

Proxy/backend processes are owned CPU fixtures with synthetic unit/provenance
views. Native MCP parsing uses actual loopback HTTP; no systemd/site/GPU rerun.
"""
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest
import yaml

from deploy.maintenance_executor import ExecutorError, digest, handle
from deploy.maintenance_native import NativeAdapter
from llmsvc.maintenance import MaintenanceError
from llmsvc.state import Pin
from test_catalog_lifecycle import catalog
import test_maintenance_lifecycle as lifecycle


@pytest.fixture
def native_integration(catalog, tmp_path, monkeypatch):
    document = yaml.safe_load(catalog.path.read_bytes())
    document['macros'] = {'llmsvc_reload_generation':'gen_'+'0'*32}
    catalog.path.write_text(yaml.safe_dump(document))
    # Add a source-derived v252 reply to the existing owned HTTP process.
    post = ''' def do_POST(self):
  request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  assert self.path=='/api/mcp' and request['params']['arguments']=={'path':'macros.llmsvc_reload_generation'}
  text='Current llama-swap configuration at "macros.llmsvc_reload_generation" (credentials redacted, values resolved):\\n\\n```yaml\\n'+info['generation']+'\\n```\\n'
  body=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'content':[{'type':'text','text':text}]}}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
'''
    monkeypatch.setattr(lifecycle, 'SERVICE', lifecycle.SERVICE.replace(' def do_GET(self):', post+' def do_GET(self):'))
    with contextmanager(lifecycle.maintenance.__wrapped__)(catalog, tmp_path) as c:
        original = c.backend.request
        adapter = NativeAdapter.__new__(NativeAdapter)
        adapter.config = c.path
        adapter.profile = {'native_origin':'http://127.0.0.1:'+str(c.backend.port),
                           'listen_host':'127.0.0.1','listen_port':c.backend.port}
        adapter.original_scope = lambda context:(c.backend.old_scope,[c.backend.old_identity])
        def retired(identity, scope, actors, context, deadline):
            matches = [(c.backend.old,c.backend.old_identity),(c.backend.new,c.backend.new_identity)]
            process = next((process for process, expected in matches if expected == identity), None)
            assert process is not None
            return process.poll() is not None, all(p.poll()==0 for p in c.backend.helpers), []
        adapter._retired = retired
        adapter.inspect_native = lambda context, deadline:original('inspect',context,deadline=deadline)
        adapter.config_data = lambda:(c.path.read_bytes(),yaml.safe_load(c.path.read_bytes()))
        adapter._attempt_binding = lambda identity,context,phase:c.backend.probe()['transaction_id']==context['transaction_id']
        adapter._known_unsubmitted_start = lambda context:c.backend.start_failure and c.backend.new is None
        def unit_exited(unit, deadline):
            name = unit[len('vllm-'):-len('.service')]
            process = c.backend.model_processes.get(name)
            return process is not None and process.poll() is not None
        adapter.unit_exited = unit_exited
        def backend(name, deadline, expected=None):
            process = c.backend.model_processes.get(name)
            observation = c.world['units'].get(name)
            if process is None or process.poll() is not None or observation is None or not observation.active:
                raise ExecutorError('fixture_backend_absent')
            actual = {'model':name,'unit':'vllm-'+name+'.service','lease_id':observation.lease_id,
                      'gpu':0,'invocation_id':observation.invocation_id}
            if expected is not None and any(expected[k]!=v for k,v in actual.items()):
                raise ExecutorError('fixture_backend_changed')
            return actual
        adapter.backend = backend
        observations = []
        def request(operation, context, *, deadline):
            if operation not in ('observe_candidate','observe_base'):
                return original(operation,context,deadline=deadline)
            envelope = {'operation':operation,'context':context,'timeout_seconds':deadline-time.monotonic()}
            envelope['request_id'] = digest(envelope)
            result = handle(envelope,operation,adapter)
            observations.append((operation,result['backends_confirmed'],result['cleanup_confirmed']))
            return result
        c.backend.request = request
        c.native_adapter, c.native_observations = adapter, observations
        yield c


def test_actual_native_observation_allows_protected_cleanup_after_adoption(native_integration):
    c = native_integration
    process, prepared = lifecycle.add_owned_model(c)
    c.runtime.enqueue(prepared, cleanup=lambda **kw:c.controller.stop_model('base',**kw))
    result = c.runtime.process_once()
    assert result['status'] == 'applied', (result, c.native_observations)
    assert ('observe_candidate',True,False) in c.native_observations
    assert c.native_observations[-1] == ('observe_candidate',True,True)
    assert process.poll() is not None and c.store.lease('unit-lease')[0].status == 'released'
    assert c.backend.calls.count('stop_model') == 1 and not c.s.catalog_fenced


def test_exit_before_account_commit_reconciles_without_replaying_stop(native_integration, monkeypatch):
    c = native_integration
    process, prepared = lifecycle.add_owned_model(c)
    release = c.store.release_maintenance_lease
    def interrupted(*args):
        raise OSError('fixture account write interrupted after exit')
    monkeypatch.setattr(c.store,'release_maintenance_lease',interrupted)
    c.runtime.enqueue(prepared,cleanup=lambda **kw:c.controller.stop_model('base',**kw))
    assert c.runtime.process_once()['status'] == 'reconciliation_required'
    assert process.poll() is not None and c.store.lease('unit-lease')[0].status == 'confirmed'
    monkeypatch.setattr(c.store,'release_maintenance_lease',release)
    c.s.sample_once()
    assert c.controller.reconcile()['status'] == 'reconciled'
    assert c.store.lease('unit-lease')[0].status == 'released'
    assert c.backend.calls.count('stop_model') == 1 and not c.s.catalog_fenced


def test_rollback_cancels_removed_model_cleanup_but_retains_pin_and_budget(native_integration):
    c = native_integration
    process, prepared = lifecycle.add_owned_model(c)
    # The native stop helper sleeps retained backends. Model this observation
    # explicitly; an awake pin still blocks source rollback under the guards.
    collect = c.s.collect
    def sleeping_observation():
        snapshot = collect()
        if c.backend.old.poll() is not None:
            snapshot = replace(snapshot, models=tuple(replace(model,state='sleeping',is_sleeping=True)
                if model.name=='base' and model.unit_active else model for model in snapshot.models))
        return snapshot
    c.s.collect = sleeping_observation
    def pin_after_start():
        c.store.put_pin(Pin('base',20000,'fixture-owner'))
        c.s.sample_once()
    c.backend.after_start = pin_after_start
    c.runtime.enqueue(prepared,cleanup=lambda **kw:c.controller.stop_model('base',**kw))
    assert c.runtime.process_once()['status'] == 'reconciliation_required'
    assert process.poll() is None and 'stop_model' not in c.backend.calls
    assert c.controller.rollback()['status'] == 'rolled_back'
    assert process.poll() is None and 'stop_model' not in c.backend.calls
    assert c.store.lease('unit-lease')[0].budget_gb == 40
    assert c.store.lease('unit-lease')[0].status == 'confirmed'
    assert c.store.active(c.clock())[0][0].model == 'base'
    assert 'base' in c.runtime.manifest['active'] and not c.s.catalog_fenced


def test_visible_candidate_cannot_release_with_unfinished_cleanup(native_integration):
    c = native_integration
    process, prepared = lifecycle.add_owned_model(c)
    c.runtime.enqueue(prepared)  # Intentionally missing removal action callback.
    assert c.runtime.process_once()['status'] == 'reconciliation_required'
    assert process.poll() is None and c.store.lease('unit-lease')[0].status == 'confirmed'
    assert c.store.lease('unit-lease')[0].budget_gb == 40
    assert c.s.catalog_fenced and c.store.catalog_pending()
    assert 'stop_model' not in c.backend.calls
