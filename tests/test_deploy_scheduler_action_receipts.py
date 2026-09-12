# Generated-By: Codex / gpt-5.6-luna
"""Focused receipt-retention tests for already-submitted action helpers."""

import json
from pathlib import Path

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
        raw=path.read_text()
        if len(raw)>262144:raise RuntimeError('too large')
        return json.loads(raw)
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


# Generated-By: Codex / gpt-5.6-luna
