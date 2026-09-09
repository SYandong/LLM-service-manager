# Generated-By: Codex / gpt-6-astra
"""Actual core/registry HTTP through standalone client; no queued jobs or writes."""
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
import pytest
from llmsvc.state import Pin
from test_registry_http_preview import mounted, registry_fixture, assert_readonly
from test_llm_events import api


def address(service):
    return 'http://%s:%s'%service.address


def test_copied_cli_actual_list_add_rm_previews_have_zero_mutation(api,mounted,tmp_path):
    script=tmp_path/'llm'
    shutil.copyfile(Path(__file__).resolve().parents[1]/'cli'/'llm',script)
    weights=mounted.weights.parent/'path with space'
    shutil.copytree(mounted.weights,weights)
    before=mounted.files(),mounted.scheduler.events_since(0)
    commands=[(['models','--json'],0),
              (['add',str(weights),'--name','ft','--base','base','--dry-run','--json'],1),
              (['rm','saved','--dry-run','--json'],1)]
    replies=[]
    for words,code in commands:
        completed=subprocess.run([sys.executable,'-I','-S',str(script),'--url',address(mounted),*words],
                                  cwd=tmp_path,capture_output=True,text=True,timeout=5)
        assert completed.returncode==code,(completed.stdout,completed.stderr)
        replies.append(json.loads(completed.stdout))
    assert replies[0]['records']==mounted.records and replies[0]['writes_enabled'] is False
    assert 'base' not in replies[0]['records']  # Listing is temporary metadata, not all service models.
    for result in replies[1:]:
        assert result['dry_run'] is True and result['config_committed'] is False and 'id' not in result
        assert {row['reason'] for row in result['blocked_by']} >= {'registry_writes_disabled','inflight_stream_unknown'}
    assert replies[1]['would']==[{'kind':'add_model','model':'ft','base':'base'}]
    assert replies[2]['would'][0]['kind']=='remove_model'
    assert_readonly(mounted,before)


@pytest.mark.parametrize('readonly',[True,False])
@pytest.mark.parametrize('words',[['rm','saved'],['add','PLACEHOLDER','--name','ft','--base','base']])
def test_actual_write_disabled_in_every_mode_keeps_structured_error(api,mounted,capsys,readonly,words):
    mounted.scheduler.config=replace(mounted.scheduler.config,read_only=readonly,
                                      state_db_path=str(mounted.path.parent/'unused-intents.sqlite'))
    words=[str(mounted.weights) if word=='PLACEHOLDER' else word for word in words]
    before=mounted.files(),mounted.scheduler.events_since(0)
    result=api['main'](['--url',address(mounted),*words,'--json'])
    assert result==1
    assert json.loads(capsys.readouterr().out)['error']==('read_only' if readonly else 'operation_not_enabled')
    assert_readonly(mounted,before)


def test_actual_unconfigured_and_reconciliation_failures_are_not_empty_success(api,mounted,monkeypatch,capsys):
    with monkeypatch.context() as patch:
        patch.setattr(mounted.scheduler,'registry',None)
        assert api['main'](['--url',address(mounted),'models','--json'])==1
        assert json.loads(capsys.readouterr().out)['error']=='registry_not_configured'
    mounted.registry.queue.marker.write_text('fixture pending transaction')
    before=mounted.files(),mounted.scheduler.events_since(0)
    assert api['main'](['--url',address(mounted),'models','--json'])==0
    listed=json.loads(capsys.readouterr().out)
    assert listed['records']==mounted.records
    assert any(b['reason']=='registry_reconciliation_required' for b in listed['blocked_by'])
    assert api['main'](['--url',address(mounted),'rm','saved','--dry-run','--json'])==1
    assert json.loads(capsys.readouterr().out)['error']=='registry_reconciliation_required'
    assert_readonly(mounted,before)


@pytest.mark.parametrize('name',['saved','bad/name','bad%2Fname','-unsafe'])
def test_protected_or_invalid_remove_preserves_owner_error_and_encoded_empty_body(api,mounted,capsys,name):
    mounted.state[0]=replace(mounted.state[0],pins=(Pin('saved',2000,'owner'),))
    mounted.scheduler.sample_once()
    before=mounted.files(),mounted.scheduler.events_since(0)
    result=api['main'](['--url',address(mounted),'rm','--dry-run','--json','--',name])
    assert result==1
    reply=json.loads(capsys.readouterr().out)
    assert reply['error']=='registry_invalid_request' and reply['message']
    if name=='saved': assert 'pinned' in reply['message']
    assert_readonly(mounted,before)
