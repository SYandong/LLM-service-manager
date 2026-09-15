# Generated-By: Codex / gpt-6-astra
# Generated-By: OpenCode / deepseek-v4.1-flash
"""Actual core/registry HTTP through standalone client; the write CLI is gone."""
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
import pytest
from test_registry_http_preview import mounted, registry_fixture, assert_readonly, request
from test_llm_events import api


def address(service):
    return 'http://%s:%s'%service.address


def test_copied_cli_actual_list_has_zero_mutation(api,mounted,tmp_path):
    script=tmp_path/'llm'
    shutil.copyfile(Path(__file__).resolve().parents[1]/'cli'/'llm',script)
    before=mounted.files(),mounted.scheduler.events_since(0)
    completed=subprocess.run([sys.executable,'-I','-S',str(script),'--url',address(mounted),'models','--json'],
                              cwd=tmp_path,capture_output=True,text=True,timeout=5)
    assert completed.returncode==0,(completed.stdout,completed.stderr)
    listed=json.loads(completed.stdout)
    assert listed['records']==mounted.records and listed['writes_enabled'] is False
    assert 'base' not in listed['records']  # Listing is temporary metadata, not all service models.
    assert_readonly(mounted,before)


@pytest.mark.parametrize('words',[['rm','saved'],['add','/x','--name','ft','--base','base'],['import','x']])
def test_removed_write_commands_exit_before_http(api,mounted,words):
    before=mounted.files(),mounted.scheduler.events_since(0)
    with pytest.raises(SystemExit):
        api['main'](['--url',address(mounted),*words,'--json'])
    assert_readonly(mounted,before)


@pytest.mark.parametrize('readonly',[True,False])
def test_actual_writes_report_removed_surface_in_every_mode(mounted,readonly):
    mounted.scheduler.config=replace(mounted.scheduler.config,read_only=readonly,
                                      state_db_path=str(mounted.path.parent/'unused-intents.sqlite'))
    before=mounted.files(),mounted.scheduler.events_since(0)
    assert request(mounted.address,'POST','/v1/models')==(405,{'error':'registry_writes_removed'})
    assert request(mounted.address,'DELETE','/v1/models/saved')==(405,{'error':'registry_writes_removed'})
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
    assert request(mounted.address,'POST','/v1/models')==(405,{'error':'registry_writes_removed'})
    assert_readonly(mounted,before)
