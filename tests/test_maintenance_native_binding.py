# Generated-By: Codex / gpt-6-astra
"""Actual queue/SQLite/process/RPC caller mount; source and grouping are fixtures."""
import copy
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

import test_maintenance_lifecycle as lifecycle
from test_catalog_lifecycle import catalog
from test_maintenance_lifecycle import maintenance, enqueue
from llmsvc.maintenance import MaintenanceController, MaintenanceError
from llmsvc.native_binding import build_bound_generation_reader
from llmsvc.reload_witness import PINNED_COMMIT, QUERY_PINNED_COMMIT

POST = ''' def do_POST(self):
  request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  args=request['params']['arguments']
  if args=={'query':'.macros.llmsvc_reload_generation'}:
   prefix='Current llama-swap configuration, jq query .macros.llmsvc_reload_generation (credentials redacted, values resolved):\\n\\n```yaml\\n'
  elif args=={'path':'macros.llmsvc_reload_generation'}:
   prefix='Current llama-swap configuration at "macros.llmsvc_reload_generation" (credentials redacted, values resolved):\\n\\n```yaml\\n'
  else: raise AssertionError(args)
  text=prefix+str(info['generation'])+'\\n```\\n'
  body=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'content':[{'type':'text','text':text}]}}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
'''


@pytest.fixture
def native_source(catalog, monkeypatch):
    document=yaml.safe_load(catalog.path.read_bytes())
    document['macros']={'llmsvc_reload_generation':'gen_'+'2'*32}
    catalog.path.write_text(yaml.safe_dump(document))
    monkeypatch.setattr(lifecycle,'SERVICE',lifecycle.SERVICE.replace(' def do_GET(self):',POST+' def do_GET(self):'))


@pytest.fixture(params=['v252-path','8fa85899-query'])
def bound(native_source, maintenance, request):
    c=maintenance
    image=hashlib.sha256(Path('/proc/'+str(c.backend.old.pid)+'/exe').read_bytes()).hexdigest()
    settings={'images':[{'source_commit':PINNED_COMMIT if request.param=='v252-path' else QUERY_PINNED_COMMIT,
        'executable_sha256':image,'dialect':request.param}],
        'phase_images':dict.fromkeys(['old','candidate','restored'],image),'request_timeout_seconds':2}
    c.s.config=replace(c.s.config,native_witness=settings)
    c.controller.native_reader=build_bound_generation_reader(c.s.config,
        instance_provider=c.controller.inspect_instance,config_reader=c.q._read,
        config_provider=lambda:c.s.config,clock=c.q.clock)
    original=c.backend.request
    def physical(operation,context,*,deadline):
        result=original(operation,context,deadline=deadline)
        if operation in ('observe_candidate','observe_base'):
            # Proposed ops field reports only its independent file checks.
            result['configuration_file_confirmed']=result.pop('configuration_confirmed')
            result.pop('generation',None)
        return result
    c.backend.request=physical
    return c


def test_actual_bound_reader_releases_only_after_independent_settlement(bound):
    c=bound;enqueue(c)
    result=c.runtime.process_once()
    assert result['status']=='applied',result
    record=c.store.maintenance_checkpoint(c.store.catalog_checkpoint()['transaction_id'])
    assert record['stage']=='released'
    assert record['native_provenance']['settings']==c.s.config.native_witness
    assert record['native_provenance']['base_generation']=='gen_'+'2'*32
    witness=record['observations']['candidate']['native_visibility']
    assert witness['pin']['dialect']==c.s.config.native_witness['images'][0]['dialect']
    assert witness['settlement_confirmed'] is None
    assert c.backend.calls.count('stop_old')==1 and c.backend.calls.count('start_candidate')==1
    assert not c.s.catalog_fenced


def test_native_visibility_does_not_replace_failed_helper_proof(bound):
    c=bound;c.backend.helper_mode='failed';c.q.operation_timeout=.5;enqueue(c)
    result=c.runtime.process_once()
    assert result['status']=='reconciliation_required'
    assert c.s.catalog_fenced and 'start_candidate' not in c.backend.calls


def test_phase_target_must_match_running_image_before_source_stop(bound):
    c=bound;other=copy.deepcopy(c.s.config.native_witness)
    fake={**other['images'][0],'executable_sha256':'a'*64}
    other['images'].append(fake);other['phase_images']['old']='a'*64
    c.s.config=replace(c.s.config,native_witness=other)
    c.controller.native_reader=build_bound_generation_reader(c.s.config,
        instance_provider=c.controller.inspect_instance,config_reader=c.q._read,
        config_provider=lambda:c.s.config,clock=c.q.clock)
    enqueue(c)
    with pytest.raises(MaintenanceError, match='visibility is unconfirmed'):
        c.runtime.process_once()
    assert c.store.catalog_checkpoint() is None
    assert 'stop_old' not in c.backend.calls and c.backend.old.poll() is None


def test_native_settings_cannot_change_across_pending_claim(bound):
    c=bound;c.backend.start_failure=True;enqueue(c)
    assert c.runtime.process_once()['status']=='reconciliation_required'
    record=c.store.maintenance_checkpoint(c.store.catalog_checkpoint()['transaction_id'])
    changed=copy.deepcopy(c.s.config.native_witness);changed['request_timeout_seconds']=3
    c.s.config=replace(c.s.config,native_witness=changed)
    with pytest.raises(MaintenanceError):c.controller.reconcile()
    assert c.store.maintenance_checkpoint(record['transaction_id'])==record
    assert c.s.catalog_fenced


def test_actual_base_reader_survives_durable_rollback(bound):
    c=bound;c.backend.start_failure=True;enqueue(c)
    assert c.runtime.process_once()['status']=='reconciliation_required'
    result=c.controller.rollback()
    assert result['status']=='rolled_back',result
    record=c.store.maintenance_checkpoint(c.store.catalog_checkpoint()['transaction_id'])
    assert record['stage']=='rolled_back' and not c.s.catalog_fenced
    assert record['rollback_identity']!=record['old_identity']


def test_visible_candidate_never_overrides_unknown_cleanup(bound):
    c=bound;request=c.backend.request
    def unsettled(operation,context,*,deadline):
        result=request(operation,context,deadline=deadline)
        if operation=='observe_candidate':result['cleanup_confirmed']=False
        return result
    c.backend.request=unsettled;enqueue(c)
    assert c.runtime.process_once()['status']=='reconciliation_required'
    assert c.backend.new.poll() is None and c.s.catalog_fenced
    assert c.store.catalog_checkpoint()['phase']!='released'


def test_provenance_survives_reopen_and_is_immutable(bound):
    from llmsvc.store import IntentStore
    c=bound;c.backend.start_failure=True;enqueue(c)
    assert c.runtime.process_once()['status']=='reconciliation_required'
    record=c.store.maintenance_checkpoint(c.store.catalog_checkpoint()['transaction_id'])
    with_store=IntentStore(c.s.config.state_db_path,action_lock=c.s.action_lock,read_only=True)
    try:assert with_store.maintenance_checkpoint(record['transaction_id'])==record
    finally:with_store.close()
    changed=copy.deepcopy(record);changed['native_provenance']['settings']['request_timeout_seconds']=3
    with pytest.raises(ValueError,match='identity changed'):c.store.save_maintenance(record,changed)
    assert c.store.maintenance_checkpoint(record['transaction_id'])==record


def test_pending_claim_cannot_disable_bound_mode_on_restart(bound):
    c=bound;c.backend.start_failure=True;enqueue(c)
    assert c.runtime.process_once()['status']=='reconciliation_required'
    c.s.config=replace(c.s.config,native_witness={})
    reloaded=MaintenanceController(c.s,c.q,c.backend);reloaded.bind(c.runtime)
    with pytest.raises(MaintenanceError):reloaded.reconcile()
    assert c.s.catalog_fenced and c.backend.calls.count('stop_old')==1


def test_untagged_base_refuses_before_any_source_effect(bound):
    c=bound
    raw=yaml.safe_load(c.path.read_bytes());raw.pop('macros');c.path.write_text(yaml.safe_dump(raw))
    enqueue(c)
    result=c.runtime.process_once()
    assert result['status']!='applied',result
    assert c.store.catalog_checkpoint() is None and 'stop_old' not in c.backend.calls
    assert c.backend.old.poll() is None


@pytest.mark.parametrize('phases', [{}, {'old':'a'*64}, {'old':'a'*64,'candidate':'a'*64,'restored':'b'*64}, []])
def test_invalid_phase_configuration_rejected_without_io(phases):
    from llmsvc.native_binding import validate_settings
    with pytest.raises(ValueError):
        validate_settings({'images':[{'source_commit':PINNED_COMMIT,'dialect':'v252-path','executable_sha256':'a'*64}],
                           'phase_images':phases})


def test_missing_base_generation_cannot_be_persisted(bound):
    from llmsvc.maintenance_state import validate_maintenance
    c=bound;c.backend.start_failure=True;enqueue(c)
    assert c.runtime.process_once()['status']=='reconciliation_required'
    record=c.store.maintenance_checkpoint(c.store.catalog_checkpoint()['transaction_id'])
    invalid=copy.deepcopy(record);invalid['native_provenance']['base_generation']=None
    with pytest.raises(ValueError,match='native phase provenance'):validate_maintenance(invalid)
    assert c.store.maintenance_checkpoint(record['transaction_id'])==record
