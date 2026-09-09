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
    reply = {**reply, 'dry_run':True, 'config_committed':False, 'blocked_by':[{'reason':'registry_writes_disabled'}]}
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


@pytest.mark.parametrize('reply', [
    {'would':[], 'dry_run':True, 'config_committed':True, 'blocked_by':[]},
    {'would':[], 'dry_run':True, 'config_committed':False, 'blocked_by':[], 'id':'unexpected-job'},
    {'status':'applied','config_committed':False,'blocked_by':[]},
])
def test_invalid_completion_or_preview_receipt_is_not_success(api,reply):
    parsed=args(api,'rm','ft',*(['--dry-run'] if 'would' in reply else []))
    with pytest.raises(api['ClientError']): api['validate_registry_result'](parsed,reply)


def test_listing_keeps_temporary_records_and_readiness_separate(api):
    parsed=args(api,'models')
    reply={'records':{'ft':{'name':'ft','base':'base','path':'/shared/ft','kind':'full_weight'}},
           'writes_enabled':False,'blocked_by':[{'reason':'registry_writes_disabled'},{'reason':'inflight_stream_unknown'}]}
    assert api['validate_registry_result'](parsed,reply)==reply
    assert api['result_exit_code'](parsed,reply)==0  # A successful read is not write readiness.
    text=api['format_result'](parsed,reply)
    assert 'not active adoption' in text and 'writes enabled: no' in text
    assert 'inflight_stream_unknown' in text and '/shared/ft' in text


@pytest.mark.parametrize('code,error', [(400,'registry_invalid_request'),(503,'registry_not_configured'),
                                       (409,'registry_reconciliation_required')])
def test_model_http_error_json_preserves_reason_and_message(api,monkeypatch,capsys,code,error):
    from urllib.error import HTTPError
    body={'error':error,'message':'owner validation detail'}
    def opener(request,**kwargs):
        raise HTTPError(request.full_url,code,error,{},io.BytesIO(json.dumps(body).encode()))
    client=api['SchedulerClient']('http://fixture.invalid',opener=opener)
    monkeypatch.setitem(api['main'].__globals__,'SchedulerClient',lambda **kwargs:client)
    result=api['main'](['--url','http://fixture.invalid','rm','ft','--dry-run','--json'])
    assert result==1 and json.loads(capsys.readouterr().out)==body


from test_registry_api import registry


def test_canonical_client_preview_reaches_real_registry_without_files_or_queue_changes(api,registry):
    # ModelRegistry is real; this protocol fixture supplies the declared core
    # envelope only. Actual SchedulerHTTPServer validation is owned by core's
    # combined #137 implementation and must be recorded separately.
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    owner,queue,weights,_,_,calls,_,_=registry
    before=(queue.path.read_bytes(),set(queue.path.parent.iterdir()),len(queue._pending),dict(queue._jobs),list(calls))
    def preview(method,path,payload=None):
        assert method=='POST' and urlsplit(path).query=='dry_run=1'
        result=owner.handle(method,urlsplit(path).path,payload,dry_run=True)
        return {**result,'dry_run':True,'config_committed':False,
                'blocked_by':[{'reason':'registry_writes_disabled'},*queue.quiet.blockers()]}
    parsed=args(api,'add',str(weights),'--name','ft','--base','base','--dry-run')
    result=api['execute_command'](parsed,SimpleNamespace(request=preview))
    assert result['would']==[{'kind':'add_model','model':'ft','base':'base'}]
    assert api['result_exit_code'](parsed,result)==1
    assert {b['reason'] for b in result['blocked_by']}=={'registry_writes_disabled','inflight_stream_unknown'}
    assert (queue.path.read_bytes(),set(queue.path.parent.iterdir()),len(queue._pending),dict(queue._jobs),list(calls))==before
