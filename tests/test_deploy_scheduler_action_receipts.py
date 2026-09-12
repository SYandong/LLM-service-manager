# Generated-By: Codex / gpt-5.6-luna
"""Focused receipt-retention tests for already-submitted action helpers."""

import json
from pathlib import Path
import time

import pytest

from deploy import scheduler_action_smoke as smoke


def _run(tmp_path):
    run=smoke.ActionRun.__new__(smoke.ActionRun)
    run.temp=str(tmp_path);run.preserve=False;run.records=[];run._submitted_receipts=[]
    run.log=lambda kind,**detail:run.records.append({'kind':kind,**detail})
    return run


def _reader(run):
    def python(_code,data,**_kwargs):
        path=Path(data['root'])/data['name']
        if not path.is_file():return None
        raw=path.read_bytes()
        if len(raw)>262144:raise RuntimeError('too large')
        return json.loads(raw.decode())
    run.python=python


def test_missing_submitted_receipt_preserves_private_root_and_marks_unknown(tmp_path):
    run=_run(tmp_path);_reader(run)
    run._submitted_requests=[{'id':'r1','operation':'free','output':'result-r1.json','unit':'u'}]
    run._capture_submitted_receipts()
    assert run.preserve is True and run._submitted_receipts==[]
    assert run.records[-1]['kind']=='submitted_outcome_unknown'


def test_matching_failed_receipt_is_captured_without_promoting_success(tmp_path):
    run=_run(tmp_path);_reader(run)
    (tmp_path/'result-r2.json').write_text(json.dumps({
        'local_request_id':'r2','operation':'wake','status':'failed',
        'error':'scheduler deadline expired','evidence':{'response':None}}))
    run._submitted_requests=[{'id':'r2','operation':'wake','output':'result-r2.json','unit':'u'}]
    run._capture_submitted_receipts()
    assert run.preserve is False and run._submitted_receipts[0]['status']=='failed'
    assert run.records[-1]['kind']=='submitted_receipt_captured'


def test_mismatched_late_receipt_remains_unknown(tmp_path):
    run=_run(tmp_path);_reader(run)
    (tmp_path/'result-r3.json').write_text(json.dumps({
        'local_request_id':'other','operation':'free','status':'passed'}))
    run._submitted_requests=[{'id':'r3','operation':'free','output':'result-r3.json','unit':'u'}]
    run._capture_submitted_receipts()
    assert run.preserve is True and run._submitted_receipts==[]


def test_control_ack_failure_after_submission_is_captured_once(tmp_path, monkeypatch):
    run=_run(tmp_path);run.attempted=True;run.model='fixture';run.unit='vllm-fixture.service'
    run.deadline=monotonic=time.monotonic()+70;run.work_deadline=monotonic-5
    run.config={'scheduler_python':'python3'};run.inventory=lambda **_:None
    run._submitted_requests=[];run._submitted_receipts=[];controls=[]
    def python(code,data,**_kwargs):
        if 'p.write_text' in code:
            Path(data['root'],data['name']).write_text(json.dumps(data['data']));return {}
        if 'p.open' in code:
            return json.loads(Path(data['root'],data['name']).read_text()) if Path(data['root'],data['name']).exists() else None
        raise AssertionError('unexpected helper code')
    def control(unit,*_args,**_kwargs):
        controls.append(unit);request=run._submitted_requests[-1]
        (tmp_path/request['output']).write_text(json.dumps({'local_request_id':request['id'],
            'operation':request['operation'],'status':'failed','error':'worker rejected'}))
        raise RuntimeError('control acknowledgement lost')
    run.python=python;run.control=control
    monkeypatch.setattr(smoke.uuid,'uuid4',lambda:type('U',(),{'hex':'request-1'})())
    with pytest.raises(RuntimeError,match='acknowledgement lost'):
        run.phase('free',1)
    assert controls==['llmsvc-ops-action-request-request-1.service']
    assert run._submitted_requests[0]['submission_state']=='unknown'
    run._capture_submitted_receipts()
    assert run._submitted_receipts[0]['status']=='failed' and run.preserve is False


# Generated-By: Codex / gpt-5.6-luna
