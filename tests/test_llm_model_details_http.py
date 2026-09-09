# Generated-By: Codex / gpt-6-astra
"""Actual #152 HTTP/copy-CLI details; all planning stays in the owner registry."""
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID
import pytest
import yaml
from llmsvc.reload import ReloadQueue
from llmsvc.state import Pin
from test_registry_http_preview import mounted,registry_fixture,assert_readonly
from test_llm_events import api


def client(api,mounted):
    return api['SchedulerClient']('http://%s:%s'%mounted.address)


def args(api,*words):
    return api['build_parser']().parse_args(list(words))


def test_copied_cli_keeps_basic_fields_and_actual_inventory_plan_details(api,mounted,tmp_path):
    script=tmp_path/'llm'
    shutil.copyfile(Path(__file__).resolve().parents[1]/'cli'/'llm',script)
    before=mounted.files(),mounted.scheduler.events_since(0)
    replies=[]
    for words,code in [(['models','--json'],0),
                       (['add',str(mounted.weights),'--name','ft','--base','base','--dry-run','--json'],1),
                       (['rm','saved','--dry-run','--json'],1)]:
        done=subprocess.run([sys.executable,'-I','-S',str(script),'--url','http://%s:%s'%mounted.address,*words],
                            cwd=tmp_path,text=True,capture_output=True,timeout=5)
        assert done.returncode==code,(done.stdout,done.stderr)
        replies.append(json.loads(done.stdout))
    listing,added,removed=replies
    assert listing['records']==mounted.records
    assert {r['name']:r['temporary'] for r in listing['inventory']['models']}=={'base':False,'saved':True}
    assert listing['inventory']['config_sha256']==hashlib.sha256(mounted.path.read_bytes()).hexdigest()
    assert added['plan']['model']['daemon_port']==8105 and added['plan']['model']['base']=='base'
    assert added['plan']['port_reserved'] is False and added['plan']['config_written'] is False
    assert set(removed['plan'])=={'projected_base_sha256','candidate_sha256','config_written'}
    for result in (added,removed):
        assert result['would'] and result['dry_run'] is True and result['config_committed'] is False
        assert {'reason':'inflight_stream_unknown'} in result['blocked_by']
        assert 'id' not in result and 'cmd' not in result['plan'] and 'candidate_bytes' not in result['plan']
    assert_readonly(mounted,before)


def test_client_reads_real_pending_fifo_projection_without_reserving_port(api,mounted,monkeypatch):
    queue=mounted.registry.queue
    with monkeypatch.context() as setup:
        setup.setattr(queue,'_stage',ReloadQueue._stage.__get__(queue))
        setup.setattr(queue,'validate',lambda path:yaml.safe_load(path.read_bytes()))
        setup.setattr('llmsvc.reload.uuid.uuid4',lambda:UUID(int=1))
        queued=mounted.registry.add({'name':'pending','path':str(mounted.weights),'base':'base'})
    before=mounted.files(),mounted.scheduler.events_since(0),queue.get(queued['id'])
    parsed=args(api,'add',str(mounted.weights),'--name','ft','--base','base','--dry-run')
    for _ in range(2):
        result=api['execute_command'](parsed,client(api,mounted))
        assert result['plan']['model']['daemon_port']==8106
        assert 'daemon port=8106 (not reserved)' in api['format_result'](parsed,result)
        assert api['result_exit_code'](parsed,result)==1
    listing=api['execute_command'](args(api,'models'),client(api,mounted))
    assert [job['id'] for job in listing['inventory']['pending_changes']]==[queued['id']]
    assert all(job['recorded_status']=='queued' for job in listing['inventory']['pending_changes'])
    assert (mounted.files(),mounted.scheduler.events_since(0),queue.get(queued['id']))==before
    assert len(queue._pending)==1 and not mounted.calls and not mounted.units


@pytest.mark.parametrize('condition',['pin','unknown','stale'])
def test_client_preserves_unknowns_protection_and_actual_remove400(api,mounted,capsys,condition):
    if condition=='pin':
        mounted.state[0]=replace(mounted.state[0],pins=(Pin('saved',mounted.clock[0]+3600,'owner'),))
        mounted.scheduler.sample_once()
    elif condition=='unknown':
        state=mounted.state[0]
        mounted.state[0]=replace(state,models=(state.models[0],replace(state.models[1],state='unknown')),
                                 activity=tuple(item for item in state.activity if item.model!='saved'))
        mounted.scheduler.sample_once()
    else:
        mounted.clock[0]+=mounted.scheduler.config.max_snapshot_age_seconds+1
    before=mounted.files(),mounted.scheduler.events_since(0)
    result=api['execute_command'](args(api,'models'),client(api,mounted))
    row=next(r for r in result['inventory']['models'] if r['name']=='saved')
    text=api['format_result'](args(api,'models'),result)
    assert row['removable'] is False and row['blocked_by']
    if condition=='unknown':
        assert row['expires_at'] is None and 'idle expiry=unknown' in text
    if condition!='pin': assert row['runtime_state']=='unknown'
    code=api['main'](['--url','http://%s:%s'%mounted.address,'rm','saved','--dry-run','--json'])
    assert code==1
    error=json.loads(capsys.readouterr().out)
    assert error['error']=='registry_invalid_request' and 'plan' not in error and 'would' not in error
    assert_readonly(mounted,before)


def test_actual_marker409_and_write405_remain_errors(api,mounted,capsys):
    mounted.registry.queue.marker.write_text('fixture interrupted marker')
    before=mounted.files(),mounted.scheduler.events_since(0)
    result=api['execute_command'](args(api,'models'),client(api,mounted))
    assert result['inventory']['fenced'] is True and result['inventory']['recovery']['settlement_confirmed'] is None
    url='http://%s:%s'%mounted.address
    for words,reason in [(['rm','saved','--dry-run'],'registry_reconciliation_required'),(['rm','saved'],'read_only')]:
        assert api['main'](['--url',url,*words,'--json'])==1
        assert json.loads(capsys.readouterr().out)['error']==reason
    assert_readonly(mounted,before)
