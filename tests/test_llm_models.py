# Generated-By: Codex / gpt-6-astra
"""Model client wire and queue semantics; actual mounted fixtures added with core contract."""
import io
import json
import pytest
from test_llm_events import api


def args(api, *words):
    return api['build_parser']().parse_args(list(words))


@pytest.mark.parametrize('words,path,body,reply', [
    (['add','/shared/path with space','--name','ft','--base','base','--dry-run'],
     '/v1/models?dry_run=1', {'name':'ft','path':'/shared/path with space','base':'base'},
     {'would':[{'kind':'add_model','model':'ft','base':'base'}]}),
    (['rm','--dry-run','--','-org/a ?#%模型'], '/v1/models/-org%2Fa%20%3F%23%25%E6%A8%A1%E5%9E%8B?dry_run=1', None,
     {'would':[{'kind':'remove_model','model':'-org/a ?#%模型'}]}),
])
def test_stable_wire_payload_and_url_encoding(api, words, path, body, reply):
    calls=[]
    def opener(request, **kwargs):
        calls.append(request)
        return io.BytesIO(json.dumps(reply).encode())
    parsed=args(api,*words)
    result=api['execute_command'](parsed,api['SchedulerClient']('http://fixture.invalid',opener=opener))
    assert result==reply and len(calls)==1
    assert calls[0].full_url=='http://fixture.invalid'+path
    assert calls[0].get_method()==('POST' if parsed.command=='add' else 'DELETE')
    assert (json.loads(calls[0].data) if calls[0].data else None)==body


@pytest.mark.parametrize('words', [['add','/x'],['add','/x','--name','n'],['add','/x','--base','b'],
    ['add','/x','--name','','--base','b'],['add','/x','--name','n','--base','b','--lora'],['rm',''],['rm','a\nb']])
def test_invalid_input_rejected_without_http(api, words):
    with pytest.raises(SystemExit): args(api,*words)


@pytest.mark.parametrize('status,committed', [('queued',False),('blocked',False),('applied',True),
    ('failed',False),('timed_out',False),('reconciliation_required',True)])
def test_structured_outcomes_do_not_confuse_queue_commit_and_apply(api,status,committed):
    parsed=args(api,'rm','ft')
    reply={'id':'job1','status':status,'config_committed':committed,
           'blocked_by':[{'reason':'quiet_unknown'}] if status=='blocked' else [],'error':None}
    assert api['validate_registry_result'](parsed,reply)==reply
    rendered=api['format_result'](parsed,reply)
    assert status in rendered and 'Config committed: '+('yes' if committed else 'no') in rendered
    assert api['result_exit_code'](parsed,reply)==(0 if status=='applied' else 1)
    if status=='queued': assert 'not applied' in rendered


@pytest.mark.parametrize('operation', ['add','rm'])
def test_uncertain_mutation_is_not_retried(api,operation):
    calls=[]
    def opener(request, **kwargs):
        calls.append(request)
        raise TimeoutError('lost reply')
    words=['add','/x','--name','n','--base','b'] if operation=='add' else ['rm','n']
    with pytest.raises(api['ClientError'],match='no automatic write retry'):
        api['execute_command'](args(api,*words),api['SchedulerClient']('http://fixture.invalid',opener=opener))
    assert len(calls)==1
