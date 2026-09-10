# Generated-By: Codex / gpt-6-astra
"""Native command contract with controlled executable and no model/service work."""
import hashlib
import time
from pathlib import Path

import pytest

from deploy.maintenance_executor import ExecutorError, ScopeInspector


def setup(tmp_path, runner):
    binary=tmp_path/'native';binary.write_bytes(b'controlled executable fixture')
    candidate=tmp_path/'candidate.yaml';candidate.write_text('models: {}\n')
    profile={'unit':'fixture.service','native_binary':str(binary),
             'native_binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
             'native_config_dir':str(tmp_path)}
    return ScopeInspector(profile,runner=runner),candidate,binary


def test_exact_native_validate_argv_and_read_only_dry_run(tmp_path):
    calls=[];i,p,binary=setup(tmp_path,lambda argv,deadline:calls.append(argv))
    checksum=hashlib.sha256(p.read_bytes()).hexdigest();before={q.name:q.read_bytes() for q in tmp_path.iterdir()}
    assert not i.validate(str(p),checksum,time.monotonic()+5,dry_run=True)['accepted']
    assert not calls
    assert i.validate(str(p),checksum,time.monotonic()+5)['accepted']
    assert calls==[[str(binary),'-config',str(p),'-validate']]
    assert {q.name:q.read_bytes() for q in tmp_path.iterdir()}==before


def test_wrong_hash_or_outside_candidate_causes_no_native_command(tmp_path):
    calls=[];i,p,_=setup(tmp_path,lambda *args:calls.append(args))
    with pytest.raises(ExecutorError,match='candidate_digest'):
        i.validate(str(p),'a'*64,time.monotonic()+5)
    other=tmp_path/'other';other.mkdir();nested=other/'candidate.yaml';nested.write_text(p.read_text())
    with pytest.raises(ExecutorError,match='outside_owned_scope'):
        i.validate(str(nested),hashlib.sha256(nested.read_bytes()).hexdigest(),time.monotonic()+5)
    assert not calls


def test_binary_pin_and_symlink_checked_before_invocation(tmp_path):
    calls=[];i,p,binary=setup(tmp_path,lambda *args:calls.append(args));checksum=hashlib.sha256(p.read_bytes()).hexdigest()
    binary.write_text('changed')
    with pytest.raises(ExecutorError,match='binary_digest'):i.validate(str(p),checksum,time.monotonic()+5)
    linked=tmp_path/'link';linked.symlink_to(p)
    with pytest.raises(ExecutorError,match='outside_owned_scope'):i.validate(str(linked),checksum,time.monotonic()+5)
    assert not calls


def test_validation_failure_or_concurrent_input_change_never_accepts(tmp_path):
    def fail(*args):raise ExecutorError('command_failed')
    i,p,_=setup(tmp_path,fail);checksum=hashlib.sha256(p.read_bytes()).hexdigest()
    with pytest.raises(ExecutorError,match='command_failed'):i.validate(str(p),checksum,time.monotonic()+5)
    i.runner=lambda *args:p.write_text('models: changed\n')
    with pytest.raises(ExecutorError,match='inputs_changed'):i.validate(str(p),checksum,time.monotonic()+5)
