# Generated-By: Codex / gpt-6-astra
"""Read-only actual registry status, preserving owner lifecycle and unknown fields."""
import json
import shutil
import subprocess
import sys
from pathlib import Path
import pytest
from llmsvc.__main__ import build_registry
from test_registry_http_status import inspection,harness,prohibit_effects,seed_marker
from test_llm_events import api


def client(api,inspection):
    return api['SchedulerClient']('http://%s:%s'%inspection.address)


def parsed(api,*options):
    return api['build_parser']().parse_args(['registry',*options])


def test_real_queue_projection_is_not_mutation_or_application(api,inspection,monkeypatch):
    q=inspection.queue
    job=q.enqueue(lambda data:data+b'# draft\n',description={'kind':'add_model','model':'ft'})
    before=q.path.read_bytes(),q.get(job['id']),list(inspection.calls),list(inspection.logs)
    prohibit_effects(inspection,monkeypatch)
    result=api['execute_command'](parsed(api),client(api,inspection))
    row=result['queue']['jobs'][0]
    assert row['status']=='blocked' and row['recorded_status']=='queued' and row['source']=='memory'
    assert row['config_committed'] is False and row['pending'] is True
    assert 'process-local monotonic' in api['format_result'](parsed(api),result)
    inspection.clock.advance(600)
    timed=api['execute_command'](parsed(api),client(api,inspection))
    assert timed['queue']['jobs'][0]['status']=='timed_out'
    assert timed['queue']['jobs'][0]['recorded_status']=='queued'
    assert len(q._pending)==1
    assert (q.path.read_bytes(),q.get(job['id']),inspection.calls,inspection.logs)==before


def test_restart_marker_nulls_remain_unknown_and_fenced(api,inspection,monkeypatch):
    seed_marker(inspection)
    old=inspection.queue
    inspection.scheduler.registry=build_registry(inspection.scheduler.config,inspection.scheduler)
    before=old.path.read_bytes(),old.marker.read_bytes(),inspection.scheduler.events_since(0)
    prohibit_effects(inspection,monkeypatch)
    result=api['execute_command'](parsed(api,'--json'),client(api,inspection))
    queue=result['queue']
    assert queue['fenced'] is True and queue['pending_ids']==[]
    row=queue['jobs'][0]
    assert row['source']=='recovery_marker' and row['status']=='reconciliation_required'
    assert row['elapsed_seconds'] is None and row['remaining_seconds'] is None and row['config_committed'] is None
    assert queue['recovery']['settlement_confirmed'] is None
    assert json.loads(api['format_result'](parsed(api,'--json'),result))==result
    text=api['format_result'](parsed(api),result)
    assert 'Queue/config fence: yes (retained)' in text and 'missing jobs do not prove application' in text
    assert (old.path.read_bytes(),old.marker.read_bytes(),inspection.scheduler.events_since(0))==before


def test_malformed_marker_and_unconfigured_state_are_not_success_claims(api,inspection,monkeypatch,capsys):
    inspection.queue.marker.write_text('{')
    before=inspection.queue.marker.read_bytes()
    prohibit_effects(inspection,monkeypatch)
    result=api['execute_command'](parsed(api),client(api,inspection))
    assert result['queue']['fenced'] is True and result['queue']['recovery']['marker_valid'] is False
    assert inspection.queue.marker.read_bytes()==before
    inspection.scheduler.registry=None
    code=api['main'](['--url','http://%s:%s'%inspection.address,'registry','--json'])
    assert code==1 and json.loads(capsys.readouterr().out)['error']=='registry_not_configured'


def test_copied_cli_inspects_without_repository_or_write_options(api,inspection,tmp_path,monkeypatch):
    prohibit_effects(inspection,monkeypatch)
    script=tmp_path/'llm'
    shutil.copyfile(Path(__file__).resolve().parents[1]/'cli'/'llm',script)
    before=inspection.queue.path.read_bytes(),inspection.scheduler.events_since(0)
    done=subprocess.run([sys.executable,'-I','-S',str(script),'--url','http://%s:%s'%inspection.address,'registry','--json'],
                        cwd=tmp_path,capture_output=True,text=True,timeout=5)
    assert done.returncode==0,done.stderr
    result=json.loads(done.stdout)
    assert result['writes_enabled'] is False and result['queue']['jobs']==[]
    assert (inspection.queue.path.read_bytes(),inspection.scheduler.events_since(0))==before
    for option in ['--reconcile','--clear','--proof','--url']:
        with pytest.raises(SystemExit): parsed(api,option)
