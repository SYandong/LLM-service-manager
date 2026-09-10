# Generated-By: Codex / gpt-6-astra
"""Bounded real subprocess protocol; no site service or GPU operation."""

import json
import sys
import time

import pytest

from llmsvc.maintenance import CommandBackend, MaintenanceError, identity


@pytest.fixture
def adapter(tmp_path):
    script = tmp_path/"adapter.py"
    script.write_text('''import json,sys,time
request=json.load(sys.stdin)
mode=sys.argv[1]
if mode == 'large':
    print('x'*100000)
elif mode == 'late':
    time.sleep(10)
else:
    print(json.dumps({'request_id': 'wrong' if mode=='unbound' else request['request_id'],
                     'transaction_id':request['context'].get('transaction_id'), 'accepted':True}))
''')
    return lambda mode: CommandBackend([sys.executable,str(script),mode])


def test_command_protocol_binds_real_response_and_does_not_use_shell(adapter):
    result=adapter("ok").request("inspect",{"transaction_id":"test"},deadline=time.monotonic()+3)
    assert result["accepted"] is True and result["transaction_id"]=="test"


@pytest.mark.parametrize("mode",["large","unbound","late"])
def test_unbounded_unbound_or_late_adapter_response_never_succeeds(adapter,mode):
    started=time.monotonic()
    with pytest.raises(MaintenanceError):
        adapter(mode).request("inspect",{"transaction_id":"test"},deadline=started+.3)
    assert time.monotonic()-started<2


@pytest.mark.parametrize("value",[None,{}, {"pid":True,"start_ticks":"1","scope_sha256":"a"*64},
    {"pid":12,"start_ticks":"unknown","scope_sha256":"a"*64},
    {"pid":12,"start_ticks":"1","scope_sha256":"?"*64}])
def test_unknown_process_identity_is_not_a_transition_proof(value):
    with pytest.raises(MaintenanceError): identity(value)


def test_command_requires_explicit_absolute_argv():
    for argv in ("/bin/sh",[],["python"],[True],["/bin/false",""]):
        with pytest.raises(ValueError): CommandBackend(argv)
