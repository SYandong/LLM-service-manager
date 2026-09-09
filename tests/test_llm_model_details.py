# Generated-By: Codex / gpt-6-astra
"""#152 client metadata consumption uses the real registry owner, never a planner clone."""
import copy
import json
from dataclasses import replace
import pytest
from llmsvc.state import Activity, ModelState, Pin
from test_registry_api import registry
from test_llm_events import api


def args(api,*words):
    return api['build_parser']().parse_args(list(words))


def inventory_reply(owner):
    return {'records':owner.records(),'writes_enabled':False,
            'blocked_by':[{'reason':'registry_writes_disabled'},{'reason':'inflight_stream_unknown'}],
            'inventory':owner.inventory()}


def preview_reply(owner,payload):
    # Contract envelope only: metadata comes from the existing real owner method.
    report=owner.preview_add(payload)
    plan={key:report[key] for key in ('model','projected_base_sha256','candidate_sha256','port_reserved','config_written')}
    return {'would':report['would'],'dry_run':True,'config_committed':False,
            'blocked_by':[{'reason':'registry_writes_disabled'},{'reason':'inflight_stream_unknown'}], 'plan':plan}


def test_owner_inventory_distinguishes_permanent_configuration_and_unknown_runtime(api,registry):
    owner,queue,_,state,clock,calls,_,_=registry
    queue.snapshot=lambda:replace(state[0],sampled_at=clock[0]-31)
    before=queue.path.read_bytes(),list(calls),queue.queue_snapshot()
    result=inventory_reply(owner)
    original=copy.deepcopy(result)
    parsed=args(api,'models')
    api['validate_registry_result'](parsed,result)
    text=api['format_result'](parsed,result)
    assert '[permanent; source=config]' in text and 'observed=unknown' in text
    assert 'idle expiry=unknown' in text and 'model eligibility is not global commit readiness' in text
    assert result['inventory']['models'][0]['temporary'] is False and result['records']=={}
    assert result==original and (queue.path.read_bytes(),calls,queue.queue_snapshot())==before
    parsed.json=True
    assert json.loads(api['format_result'](parsed,result))==original


def test_real_fifo_port_plan_is_repeated_without_reserving_writing_or_applying(api,registry):
    owner,queue,weights,_,_,calls,_,_=registry
    first=owner.add({'name':'pending','path':str(weights),'base':'base'})
    before=queue.path.read_bytes(),list(calls),queue.queue_snapshot()
    payload={'name':'ft','path':str(weights),'base':'base'}
    parsed=args(api,'add',str(weights),'--name','ft','--base','base','--dry-run')
    for _ in range(2):
        result=preview_reply(owner,payload)
        api['validate_registry_result'](parsed,result)
        assert result['plan']['model']['daemon_port']==8103
        assert result['plan']['port_reserved'] is False and result['plan']['config_written'] is False
        text=api['format_result'](parsed,result)
        assert 'daemon port=8103 (not reserved)' in text and 'base=base' in text
        assert 'configured metadata, not measured memory or allocated budget' in text
        assert 'hashes are not adoption/settlement' in text and 'config written=no' in text
        assert api['result_exit_code'](parsed,result)==1
        assert 'cmd' not in result['plan']['model']
    assert queue.queue_snapshot()['pending_ids']==[first['id']]
    assert (queue.path.read_bytes(),calls,queue.queue_snapshot())==before


def test_temporary_expiry_and_pin_protection_preserve_global_blockers(api,registry):
    owner,queue,weights,state,clock,calls,_,drain=registry
    owner.add({'name':'ft','path':str(weights),'base':'base'})
    drain()  # Fixture-only setup; all callbacks are synthetic.
    state[0]=replace(state[0],models=state[0].models+(ModelState('ft',state='stopped'),),
                     activity=state[0].activity+(Activity('ft',last_request_at=clock[0],in_flight=0),),
                     pins=(Pin('ft',clock[0]+3600,'owner'),))
    before=queue.path.read_bytes(),list(calls)
    result=inventory_reply(owner)
    parsed=args(api,'models')
    api['validate_registry_result'](parsed,result)
    row=next(row for row in result['inventory']['models'] if row['name']=='ft')
    assert row['expires_at']==clock[0]+7*86400 and row['removable'] is False
    text=api['format_result'](parsed,result)
    assert '[temporary; source=config]' in text and 'protection=pinned' in text
    assert 'not scheduled removal' in text and 'inflight_stream_unknown' in text
    assert (queue.path.read_bytes(),calls)==before


def test_basic_responses_remain_compatible_without_optional_details(api):
    listed={'records':{},'writes_enabled':False,'blocked_by':[]}
    preview={'would':[{'kind':'remove_model','model':'ft'}],'dry_run':True,'config_committed':False,'blocked_by':[]}
    assert api['validate_registry_result'](args(api,'models'),listed)==listed
    assert api['validate_registry_result'](args(api,'rm','ft','--dry-run'),preview)==preview


@pytest.mark.parametrize('change',[{'inventory':None},{'inventory':{}}, {'inventory':{'models':'bad'}}])
def test_malformed_supplied_inventory_is_explicit_not_an_empty_list(api,change):
    result={'records':{},'writes_enabled':False,'blocked_by':[],**change}
    with pytest.raises(api['ClientError'],match='optional registry inventory'):
        api['validate_registry_result'](args(api,'models'),result)


@pytest.mark.parametrize('change',[{'plan':None},{'plan':{'config_written':True}},
    {'plan':{'port_reserved':True}}, {'plan':{'candidate_bytes':'PRIVATE_MARKER'}},
    {'model':{'name':'ft','base':'base','daemon_port':True,'util_macro':'.3'}},
    {'model':{'name':'ft','base':'base','daemon_port':8103,'util_macro':'.3','cmd':'PRIVATE_MARKER'}}])
def test_malformed_or_effect_claiming_plan_is_rejected(api,registry,change):
    owner,_,weights,*_=registry
    result=preview_reply(owner,{'name':'ft','path':str(weights),'base':'base'})
    if 'plan' in change: result.update(change)
    else: result['plan'].update(change)
    with pytest.raises(api['ClientError'],match='optional registry plan'):
        api['validate_registry_result'](args(api,'add',str(weights),'--name','ft','--base','base','--dry-run'),result)


def test_unknown_plan_metadata_remains_null_and_is_not_zero(api,registry):
    owner,_,weights,*_=registry
    result=preview_reply(owner,{'name':'ft','path':str(weights),'base':'base'})
    result['plan']['model'].update(daemon_port=None,util_macro=None)
    result['plan']['candidate_sha256']=None
    parsed=args(api,'add',str(weights),'--name','ft','--base','base','--dry-run')
    api['validate_registry_result'](parsed,result)
    assert 'daemon port=unknown' in api['format_result'](parsed,result)
    parsed.json=True
    assert json.loads(api['format_result'](parsed,result))['plan']['model']['daemon_port'] is None


@pytest.mark.parametrize('change',[{'expires_at':float('inf')},{'created_at':True},{'temporary':None},
    {'removable':'yes'},{'source':'runtime'},{'runtime_state':None},{'cmd':'PRIVATE_MARKER'}])
def test_invalid_inventory_row_is_not_given_configuration_or_runtime_authority(api,registry,change):
    result=inventory_reply(registry[0])
    result['inventory']['models'][0].update(change)
    with pytest.raises(api['ClientError'],match='optional registry inventory'):
        api['validate_registry_result'](args(api,'models'),result)
