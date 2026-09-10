# Generated-By: Codex / gpt-6-astra
"""Outbound pull verification using in-memory responses and owned temp archives."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

from deploy import pull_release
from deploy.upgrade import Error
from test_deploy_upgrade import make_bundle


def release_fixture(tmp_path):
    directory=make_bundle(tmp_path/'payload');spec=json.loads((directory/'deployment.json').read_text());tag=spec['tag'];repo='example/project'
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as tar:
        for p in sorted(directory.rglob('*')):tar.add(p,arcname=str(p.relative_to(directory)),recursive=False)
    archive=stream.getvalue();digest=hashlib.sha256(archive).hexdigest()
    manifest=json.dumps({'tag':tag,'commit':'a'*40,'python_version':spec['version'],'assets':{'deployment.tar.gz':{'sha256':digest,'bytes':len(archive)}}}).encode()
    sums=(hashlib.sha256(manifest).hexdigest()+'  release-manifest.json\n'+digest+'  deployment.tar.gz\n').encode()
    base='https://github.com/'+repo+'/releases/download/'+tag+'/'
    release={'draft':False,'tag_name':tag,'published_at':'2026-01-01T00:00:00Z','assets':[{'name':n,'browser_download_url':base+n} for n in ('SHA256SUMS','release-manifest.json','deployment.tar.gz')]}
    api='https://api.github.com/repos/'+repo
    responses={api+'/releases/tags/'+tag:json.dumps(release).encode(),api+'/releases?per_page=10':json.dumps([release]).encode(),api+'/git/ref/tags/'+tag:json.dumps({'object':{'type':'tag','sha':'b'*40}}).encode(),api+'/git/tags/'+'b'*40:json.dumps({'object':{'type':'commit','sha':'a'*40}}).encode(),base+'SHA256SUMS':sums,base+'release-manifest.json':manifest,base+'deployment.tar.gz':archive}
    settings={'repo':repo,'cache_dir':str(tmp_path/'cache'),'container':'fixture','container_bundle_root':'/opt/inputs','container_settings':'/etc/llmsvc/upgrade.json','upgrade_program':'/opt/deployer/deploy/upgrade.py','lxc_prefix':['lxc']}
    calls=[]
    def get(url,limit):assert len(responses[url])<=limit;return responses[url]
    def run(args,**kw):calls.append(args);return subprocess.CompletedProcess(args,0,'{"status":"committed"}','')
    return settings,tag,responses,get,run,calls


def test_dryrun_has_no_network_files_or_commands(tmp_path):
    settings,tag,_,_,_,_=release_fixture(tmp_path)
    result=pull_release.pull(settings,tag,dry_run=True,get=lambda *a:pytest.fail('network'),run=lambda *a,**k:pytest.fail('command'))
    assert result['dry_run'] and not Path(settings['cache_dir']).exists()


def test_verified_tag_download_is_single_scoped_upgrade(tmp_path):
    settings,tag,_,get,run,calls=release_fixture(tmp_path)
    result=pull_release.pull(settings,tag,get=get,run=run)
    assert result['status']=='deployed'
    assert all(call[:3]==['lxc','exec','fixture'] or call[:3]==['lxc','file','push'] for call in calls)
    assert len([c for c in calls if 'apply' in c])==1
    assert all('profile' not in c and '/usr/local/bin/llm' not in c for c in calls)
    transfer=next(c for c in calls if c[:3]==['lxc','file','push'])
    assert Path(transfer[-2]).name.startswith('0.1.0a9-')
    count=len(calls)
    assert pull_release.pull(settings,tag,get=get,run=run)['status']=='already_deployed'
    assert len(calls)==count


def test_release_tag_commit_mismatch_never_executes(tmp_path):
    settings,tag,responses,get,run,calls=release_fixture(tmp_path)
    url='https://api.github.com/repos/example/project/git/tags/'+'b'*40
    responses[url]=json.dumps({'object':{'type':'commit','sha':'c'*40}}).encode()
    with pytest.raises(Error,match='tag commit'):pull_release.pull(settings,tag,get=get,run=run)
    assert not calls


def test_corrupt_download_never_executes(tmp_path):
    settings,tag,responses,get,run,calls=release_fixture(tmp_path)
    key=next(k for k in responses if k.endswith('/deployment.tar.gz'));responses[key]+=b'corruption'
    with pytest.raises(Error,match='hash/size'):pull_release.pull(settings,tag,get=get,run=run)
    assert not calls


def test_failed_remote_upgrade_preserves_receipt_and_does_not_auto_retry(tmp_path):
    settings,tag,_,get,run,calls=release_fixture(tmp_path)
    def fail(args,**kw):
        if 'apply' in args:return subprocess.CompletedProcess(args,1,'','failure')
        return run(args,**kw)
    with pytest.raises(Error,match='command failed'):pull_release.pull(settings,tag,get=get,run=fail)
    receipts=[p for p in Path(settings['cache_dir']).glob('*.json')]
    assert len(receipts)==1 and json.loads(receipts[0].read_text())['status']=='failed'
    with pytest.raises(Error,match='reconciliation'):pull_release.pull(settings,tag,get=get,run=run)


@pytest.mark.parametrize('kind',['symlink','hardlink','traversal','duplicate','extra'])
def test_unsafe_tar_members_refused(tmp_path,kind):
    p=tmp_path/'unsafe.tar.gz'
    with tarfile.open(p,'w:gz') as tar:
        member=tarfile.TarInfo('../escape' if kind=='traversal' else ('evil.py' if kind=='extra' else 'llm'))
        if kind in ('symlink','hardlink'):
            member.type=tarfile.SYMTYPE if kind=='symlink' else tarfile.LNKTYPE;member.linkname='/outside'
        else:member.size=1
        tar.addfile(member,None if kind in ('symlink','hardlink') else io.BytesIO(b'x'))
        if kind=='duplicate':tar.addfile(member,io.BytesIO(b'x'))
    with pytest.raises(Error):pull_release.unpack(p,tmp_path/'unpacked')
    assert not (tmp_path/'escape').exists()


def test_cross_repository_asset_and_draft_release_refused(tmp_path):
    settings,tag,responses,get,run,calls=release_fixture(tmp_path)
    url='https://api.github.com/repos/example/project/releases/tags/'+tag
    release=json.loads(responses[url]);release['draft']=True;responses[url]=json.dumps(release).encode()
    with pytest.raises(Error,match='draft'):pull_release.pull(settings,tag,get=get,run=run)
    release['draft']=False;release['assets'][0]['browser_download_url']='https://evil.invalid/asset';responses[url]=json.dumps(release).encode()
    with pytest.raises(Error,match='configured release'):pull_release.pull(settings,tag,get=get,run=run)
    assert not calls


def test_host_units_render_configured_paths_without_starting_service(tmp_path):
    settings,_,_,_,_,_=release_fixture(tmp_path)
    settings.update(host_python='/usr/bin/python3',host_program='/opt/owned/deploy/pull_release.py',poll_interval_seconds=600)
    path=tmp_path/'site.json';path.write_text(json.dumps(settings));out=tmp_path/'units'
    assert pull_release.main(['--settings',str(path),'--render-systemd',str(out),'--dry-run'])==0
    assert not out.exists()
    assert pull_release.main(['--settings',str(path),'--render-systemd',str(out)])==0
    assert 'OnUnitInactiveSec=600s' in (out/'llmsvc-release-pull.timer').read_text()
    assert '/opt/owned/deploy/pull_release.py --settings '+str(path) in (out/'llmsvc-release-pull.service').read_text()
    assert 'systemctl' not in (out/'llmsvc-release-pull.service').read_text()
