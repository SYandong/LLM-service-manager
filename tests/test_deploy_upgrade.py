# Generated-By: Codex / gpt-6-astra
"""Transactional deployment behavior, with real files/launchers and no systemd."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import zipfile

import pytest

from deploy import manage
from deploy.upgrade import Error, Upgrade, atomic, bundle, json_write, link_value, sha


def make_bundle(path, version='0.1.0a9', scope='read_only'):
    path.mkdir();(path/'wheelhouse').mkdir()
    cli=("#!/usr/bin/env python3\nimport sys\nif '--hold' in sys.argv:\n print('ready',flush=True);input();import late_module;print(late_module.VERSION)\nelse: print("+repr(version)+")\n").encode()
    (path/'llm').write_bytes(cli)
    name='wheelhouse/llmsvc-'+version+'-py3-none-any.whl'
    with zipfile.ZipFile(path/name,'w') as z:
        z.writestr('llmsvc-'+version+'.dist-info/METADATA','Metadata-Version: 2.1\nName: llmsvc\nVersion: '+version+'\n')
        z.writestr('llmsvc-'+version+'.data/scripts/llm',b'#!python\n'+cli.split(b'\n',1)[1])
    pip='wheelhouse/pip-1-py3-none-any.whl';(path/pip).write_bytes(b'fixture-pip-not-executed')
    info={'schema_version':1,'tag':'v'+version,'version':version,'commit':'a'*40,'scope':scope,
          'app_wheel':name,'cli':'llm','bootstrap_pip':pip,'install_wheels':[name],
          'files':{n:sha((path/n).read_bytes()) for n in (name,'llm',pip)}}
    (path/'deployment.json').write_text(json.dumps(info));return path


@pytest.fixture
def site(tmp_path):
    root=tmp_path/'root';root.mkdir()
    cfg={'prefix':'/opt/scheduler','shared_dir':'/srv/cli','trampoline_path':'/usr/local/bin/llm',
         'python':sys.executable,'scheduler_url':'http://127.0.0.1:8011','swap_url':'http://127.0.0.1:8000'}
    settings={'prefix':cfg['prefix'],'unit_path':'/etc/systemd/system/llmsvc-scheduler.service',
              'config_path':'/etc/llmsvc/config.yaml','cli_path':'/srv/cli/llm','state_dir':'/var/lib/llmsvc',
              'python':sys.executable,'user':'root','backup_files':['/etc/llama-swap/config.yaml','/usr/local/sbin/vllm-launch','/usr/local/sbin/vllm-reaper']}
    def write(name,data):
        p=root/name.lstrip('/');p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data);return p
    prefix=root/'opt/scheduler';prefix.mkdir(parents=True)
    write(settings['unit_path'],manage.unit_text(settings))
    config=write(settings['config_path'],json.dumps({'read_only':True,'operator_meminfo':None}))
    write(settings['cli_path'],"print('0.1.0a8')\n")
    write('/srv/cli/bin/llm-run','#!/bin/sh\nexec '+sys.executable+' '+str(root/'srv/cli/llm')+' "$@"\n').chmod(0o755)
    write(cfg['trampoline_path'],'fixed-file-bind-never-replaced')
    backups=[]
    for i,name in enumerate(settings['backup_files']):
        original=write(name,'original data plane '+str(i));saved=write('/opt/scheduler/backup/'+str(i),original.read_text())
        backups.append({'path':name,'file':str(i),'sha256':sha(saved.read_bytes()),'mode':0o644})
    manifest={'schema_version':1,'root':str(root),'settings':settings,'directories':[str(prefix)],'backups':backups,
              'files':[{'path':settings[k],'sha256':sha((root/settings[k].lstrip('/')).read_bytes())} for k in ('unit_path','config_path','cli_path')]}
    json_write(prefix/'manifest.json',manifest)
    # A legitimate site edit after install must survive upgrade AND rollback.
    config.write_text(json.dumps({'read_only':True,'operator_meminfo':'/run/real-host/meminfo'}))
    write('/opt/scheduler/venv/keep','old scheduler env')
    write('/srv/cli/venv/keep','old tui env')
    return root,cfg,settings


class Staged(Upgrade):
    def prepare(self,directory,payload):
        generation=self.shared/'releases'/payload['generation']
        if generation.exists():return super().prepare(directory,payload)
        (generation/'venv/bin').mkdir(parents=True)
        (generation/'venv/bin/python').symlink_to(sys.executable)
        atomic(generation/'llm',(directory/payload['cli']).read_bytes(),0o755)
        (generation/'late_module.py').write_text('VERSION='+repr(payload['version'])+'\n')
        json_write(generation/'release.json',{'bundle_sha256':payload['bundle_sha256'],'runtime_files':self.fingerprint(generation)})
        return generation
    def preflight(self,generation,transaction):
        data=json.loads(self.config.read_text())
        if data['read_only'] is not True:raise Error('manual approval required')
        return data


def watched(site):
    root,_,settings=site
    names=[settings[k] for k in ('unit_path','config_path','cli_path')]+['/srv/cli/bin/llm-run','/usr/local/bin/llm','/opt/scheduler/manifest.json']+settings['backup_files']
    return {name:(root/name.lstrip('/')).read_bytes() for name in names}


def test_dryrun_validates_but_creates_no_files_or_subprocess(site,tmp_path,monkeypatch):
    root,cfg,_=site;candidate=make_bundle(tmp_path/'bundle');before=sorted(str(p) for p in root.rglob('*'))
    obj=Staged(cfg,root);obj.run=lambda *a,**kw:pytest.fail('dryrun must not execute')
    assert obj.upgrade(candidate,dry_run=True)['dry_run']
    assert sorted(str(p) for p in root.rglob('*'))==before


def test_success_then_rollback_preserves_operator_config_and_fixed_file(site,tmp_path):
    root,cfg,settings=site;candidate=make_bundle(tmp_path/'bundle');before=watched(site)
    trampoline=root/cfg['trampoline_path'].lstrip('/');inode=trampoline.stat().st_ino
    obj=Staged(cfg,root);result=obj.upgrade(candidate)
    assert result['status']=='committed'
    assert json.loads((root/'opt/scheduler/manifest.json').read_text())['schema_version']==2
    assert (root/settings['config_path'].lstrip('/')).read_bytes()==before[settings['config_path']]
    assert trampoline.stat().st_ino==inode
    assert obj.health('0.1.0a9')=='0.1.0a9'
    assert Staged(cfg,root).rollback(result['transaction'])['status']=='rolled_back'
    assert watched(site)==before
    assert link_value(root/'srv/cli/current') is None
    assert (root/'opt/scheduler/venv/keep').read_text()=='old scheduler env'
    assert (root/'srv/cli/venv/keep').read_text()=='old tui env'
    assert (root/'srv/cli/releases'/result['generation']/'venv/bin/python').exists()


def test_second_version_upgrade_and_running_old_client_survives(site,tmp_path):
    root,cfg,_=site
    first=Staged(cfg,root).upgrade(make_bundle(tmp_path/'a','0.1.0a9'))
    proc=subprocess.Popen([sys.executable,str(root/'srv/cli/llm'),'--hold'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert select.select([proc.stdout],[],[],5)[0]
        assert proc.stdout.readline().strip()=='ready'
        second_runner=Staged(cfg,root)
        second=second_runner.upgrade(make_bundle(tmp_path/'b','0.1.0a10'))
        assert second_runner.health('0.1.0a10')=='0.1.0a10'
        output,err=proc.communicate('\n',timeout=5)
        assert proc.returncode==0 and output.strip()=='0.1.0a9',err
        rollback_runner=Staged(cfg,root)
        rollback_runner.rollback(second['transaction'])
        assert rollback_runner.health('0.1.0a9')=='0.1.0a9'
        assert link_value(root/'srv/cli/current')=='releases/'+first['generation']
    finally:
        if proc.poll() is None:proc.kill();proc.communicate(timeout=5)


@pytest.mark.parametrize('failure',['restart','health'])
def test_failed_cutover_rolls_back_actual_previous_files(site,tmp_path,failure):
    root,cfg,_=site;before=watched(site);obj=Staged(cfg,root)
    old=getattr(obj,failure);calls=[]
    def fail_once(*args,**kw):
        calls.append(True)
        if len(calls)==1:raise Error('injected '+failure+' failure')
        return old(*args,**kw)
    setattr(obj,failure,fail_once)
    with pytest.raises(Error,match='injected'):obj.upgrade(make_bundle(tmp_path/'bundle'))
    assert watched(site)==before
    state=json.loads((root/'opt/scheduler/upgrade-state.json').read_text());assert state['status']=='rolled_back'
    assert len(calls)==2


def test_prepare_failure_and_operator_edit_race_do_not_switch(site,tmp_path):
    root,cfg,settings=site;obj=Staged(cfg,root)
    def preflight(*args):
        obj.config.write_text('new concurrent operator change')
    obj.preflight=preflight
    with pytest.raises(Error,match='site changed'):obj.upgrade(make_bundle(tmp_path/'bundle'))
    assert link_value(root/'srv/cli/current') is None
    assert obj.config.read_text()=='new concurrent operator change'
    assert json.loads((root/'opt/scheduler/upgrade-state.json').read_text())['status']=='failed_before_switch'


def test_mutating_site_requires_approval_before_switch(site,tmp_path):
    root,cfg,settings=site;(root/settings['config_path'].lstrip('/')).write_text('{"read_only":false}')
    with pytest.raises(Error,match='manual approval'):Staged(cfg,root).upgrade(make_bundle(tmp_path/'bundle'))
    assert link_value(root/'srv/cli/current') is None


def test_runtime_and_backup_integrity_not_bypassed(site,tmp_path):
    root,cfg,settings=site;candidate=make_bundle(tmp_path/'bundle')
    (root/'opt/scheduler/backup/0').write_text('tampered')
    with pytest.raises(Error,match='backup integrity'):Staged(cfg,root).upgrade(candidate)
    assert not (root/'opt/scheduler/upgrade.lock').exists()


def test_external_config_edit_blocks_rollback_without_overwrite(site,tmp_path):
    root,cfg,_=site;obj=Staged(cfg,root);result=obj.upgrade(make_bundle(tmp_path/'bundle'))
    obj.config.write_text('new owner configuration')
    with pytest.raises(Error,match='file conflict'):Staged(cfg,root).rollback(result['transaction'])
    assert obj.config.read_text()=='new owner configuration'
    assert link_value(obj.pointer)=='releases/'+result['generation']


def test_interrupted_switch_recovery_and_stale_transaction_refusal(site,tmp_path):
    root,cfg,_=site;obj=Staged(cfg,root);result=obj.upgrade(make_bundle(tmp_path/'a'))
    state=root/'opt/scheduler/upgrade-state.json'
    json_write(state,{'transaction':result['transaction'],'status':'switching'})
    with pytest.raises(Error,match='unfinished'):Staged(cfg,root).upgrade(make_bundle(tmp_path/'b','0.1.0a10'))
    assert Staged(cfg,root).rollback(result['transaction'])['status']=='rolled_back'
    with pytest.raises(Error,match='invalid transaction'):Staged(cfg,root).rollback('../../elsewhere')


def test_pointer_escape_and_bundle_tamper_refused(site,tmp_path):
    root,cfg,_=site;candidate=make_bundle(tmp_path/'bundle');(candidate/'llm').write_text('modified')
    with pytest.raises(Error,match='hash'):Staged(cfg,root).upgrade(candidate,dry_run=True)
    (root/'srv/cli/current').symlink_to('/outside')
    with pytest.raises(Error,match='escapes'):link_value(root/'srv/cli/current')


def test_manual_bundle_class_refused_without_mutation(site,tmp_path):
    root,cfg,_=site
    with pytest.raises(Error,match='manual approval'):Staged(cfg,root).upgrade(make_bundle(tmp_path/'bundle',scope='model_actions'))
    assert not (root/'opt/scheduler/upgrade.lock').exists()


def test_automatic_downgrade_rejected_but_explicit_rollback_works(site,tmp_path):
    root,cfg,_=site;first=Staged(cfg,root).upgrade(make_bundle(tmp_path/'first','0.1.0a10'))
    with pytest.raises(Error,match='downgrade'):Staged(cfg,root).upgrade(make_bundle(tmp_path/'older','0.1.0a9'))
    assert Staged(cfg,root).rollback(first['transaction'])['status']=='rolled_back'


def test_symlinked_transaction_root_is_not_written(site,tmp_path):
    root,cfg,_=site;outside=tmp_path/'outside';outside.mkdir()
    (root/'opt/scheduler/transactions').symlink_to(outside,target_is_directory=True)
    with pytest.raises(Error,match='symlink'):Staged(cfg,root).upgrade(make_bundle(tmp_path/'bundle'))
    assert not list(outside.iterdir())


def test_modified_managed_dispatcher_is_not_adopted(site,tmp_path):
    root,cfg,_=site;Staged(cfg,root).upgrade(make_bundle(tmp_path/'first'))
    (root/'srv/cli/bin/llm-run').write_text('modified dispatcher')
    with pytest.raises(Error,match='dispatcher integrity'):Staged(cfg,root).upgrade(make_bundle(tmp_path/'second','0.1.0a10'))


def test_failed_preparation_keeps_prior_rollback_and_chain_is_reversible(site,tmp_path):
    root,cfg,_=site;before=watched(site)
    first=Staged(cfg,root).upgrade(make_bundle(tmp_path/'a','0.1.0a9'))
    bad=Staged(cfg,root)
    def fail(*args):raise Error('candidate failed before switch')
    bad.preflight=fail
    with pytest.raises(Error,match='candidate failed'):bad.upgrade(make_bundle(tmp_path/'b','0.1.0a10'))
    assert json.loads((root/'opt/scheduler/upgrade-state.json').read_text())['transaction']==first['transaction']
    second=Staged(cfg,root).upgrade(make_bundle(tmp_path/'c','0.1.0a11'))
    assert Staged(cfg,root).rollback(second['transaction'])['status']=='rolled_back'
    assert Staged(cfg,root).rollback(second['transaction'])['status']=='already_rolled_back'
    assert Staged(cfg,root).rollback(first['transaction'])['status']=='rolled_back'
    assert watched(site)==before
