# Generated-By: Codex / gpt-6-astra
"""Standalone unreserve against actual scheduler DELETE and disposable intents."""
import json
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

import pytest

from llmsvc.state import Reserve
from test_llm_pin import pin_api, pin_service, command


@pytest.mark.parametrize('identifier', ['reservation-id','a/b ?#%模型','-leading'])
def test_real_empty_body_encoded_delete_is_idempotent_and_authoritative(pin_api,pin_service,identifier):
    service=pin_service
    service.store.put_reserve(Reserve(identifier,0,80,time.time()+3600,'original-owner'))
    service.store.put_reserve(Reserve('keep',1,20,time.time()+3600,'other-owner'))
    client=pin_api['SchedulerClient'](service.url)
    opener=client.opener
    def tracked(request, **kwargs):
        assert request.data is None
        request.add_header('X-Forwarded-For','192.0.2.33')
        return opener(request,**kwargs)
    client.opener=tracked
    parsed=command(pin_api,'unreserve','--',identifier)
    for _ in range(2):
        result=pin_api['execute_command'](parsed,client)
        assert result=={'id':identifier,'by':'actual-owner'}
        assert pin_api['result_exit_code'](parsed,result)==0
        assert 'actor actual-owner' in pin_api['format_result'](parsed,result)
    assert [r.id for r in service.scheduler.snapshot().reserves]==['keep']
    assert service.requests==[('DELETE','/v1/reserve/'+quote(identifier,safe=''),0)]*2


def test_readonly_preview_zero_writes_and_actual_405(pin_api,pin_service,monkeypatch):
    service=pin_service
    service.store.put_reserve(Reserve('r',0,80,time.time()+3600,'owner'))
    service.scheduler.config=replace(service.scheduler.config,read_only=True)
    before=service.database.read_bytes(),service.scheduler.snapshot(),service.scheduler.events_since(0)
    monkeypatch.setattr(service.store,'_write',lambda *a,**k:pytest.fail('unexpected write'))
    client=pin_api['SchedulerClient'](service.url)
    result=pin_api['execute_command'](command(pin_api,'unreserve','r','--dry-run'),client)
    assert result=={'would':[{'kind':'unreserve','id':'r'}],'blocked_by':[]}
    assert (service.database.read_bytes(),service.scheduler.snapshot(),service.scheduler.events_since(0))==before
    with pytest.raises(pin_api['ClientError']) as exc:
        pin_api['execute_command'](command(pin_api,'unreserve','r'),client)
    assert exc.value.status==405 and 'read_only' in str(exc.value)
    assert service.database.read_bytes()==before[0]


@pytest.mark.parametrize('words',[['unreserve'],['unreserve',''],['unreserve','x\ny']])
def test_invalid_id_is_rejected(pin_api,words,capsys):
    with pytest.raises(SystemExit): command(pin_api,*words)
    if len(words)>1:
        assert "Reservation ID" in capsys.readouterr().err


def test_actual_lost_delete_reply_is_not_retried(pin_api,pin_service):
    pin_service.store.put_reserve(Reserve('r',0,80,time.time()+3600,'owner'))
    client=pin_api['SchedulerClient'](pin_service.url)
    opener=client.opener
    def lost(request,**kwargs):
        with opener(request,**kwargs) as response: response.read()
        raise TimeoutError('lost completed deletion reply')
    client.opener=lost
    with pytest.raises(pin_api['ClientError'],match='no automatic write retry'):
        pin_api['execute_command'](command(pin_api,'unreserve','r'),client)
    assert not pin_service.scheduler.snapshot().reserves
    assert len(pin_service.requests)==1


def test_copied_cli_deletes_without_repository_packages(pin_service,tmp_path):
    pin_service.store.put_reserve(Reserve('r',0,80,time.time()+3600,'owner'))
    script=tmp_path/'llm'
    shutil.copyfile(Path(__file__).resolve().parents[1]/'cli'/'llm',script)
    done=subprocess.run([sys.executable,'-I','-S',str(script),'--url',pin_service.url,'unreserve','r','--json'],
                        cwd=tmp_path,capture_output=True,text=True,timeout=5)
    assert done.returncode==0,done.stderr
    assert json.loads(done.stdout)=={'id':'r','by':'actual-owner'}
    assert not pin_service.scheduler.snapshot().reserves
