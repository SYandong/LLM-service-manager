#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""One outbound release poll; verify an offline bundle and invoke the sole deployer."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import quote, urlsplit
import urllib.request

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.upgrade import Error, bundle, json_write, sha
from deploy.manage import emit


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parts=urlsplit(newurl)
        if parts.scheme!='https' or parts.hostname not in ('github.com','api.github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'):
            raise Error('untrusted release download redirect')
        return super().redirect_request(request,fp,code,msg,headers,newurl)


def fetch(url, limit):
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.hostname not in ('api.github.com','github.com') or parts.username or parts.password:
        raise Error('untrusted release URL')
    request=urllib.request.Request(url,headers={'User-Agent':'llmsvc-readonly-pull','Accept':'application/vnd.github+json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),GitHubRedirect())
    end=time.monotonic()+60
    chunks=[];size=0
    with opener.open(request,timeout=10) as response:
        while True:
            chunk=response.read(65536)
            if not chunk:break
            size+=len(chunk)
            if size>limit or time.monotonic()>end:raise Error('release download size/deadline exceeded')
            chunks.append(chunk)
    return b''.join(chunks)


def checked_asset(release, name, repo, tag):
    rows=[x for x in release.get('assets',[]) if x.get('name')==name]
    if len(rows)!=1:raise Error('release missing unique asset: '+name)
    row=rows[0];expected='https://github.com/'+repo+'/releases/download/'+quote(tag,safe='')+'/'+name
    if row.get('browser_download_url')!=expected:raise Error('asset does not belong to configured release')
    return row


def checksums(raw):
    found={}
    for line in raw.decode('ascii').splitlines():
        match=re.fullmatch(r'([0-9a-f]{64}) [ *]([A-Za-z0-9_.+-]+)',line)
        if not match or match[2] in found:raise Error('invalid checksum manifest')
        found[match[2]]=match[1]
    return found


def unpack(archive, destination):
    if destination.exists() or destination.is_symlink():raise Error('fresh extraction directory required')
    destination.mkdir(mode=0o700)
    seen=set();total=0
    with tarfile.open(archive,'r:gz') as tar:
        for member in tar:
            name=str(PurePosixPath(member.name))
            p=PurePosixPath(member.name)
            if p.is_absolute() or '..' in p.parts or name in seen:raise Error('unsafe or duplicate archive entry')
            seen.add(name)
            if len(seen)>200 or not (member.isdir() or member.isfile()):raise Error('archive member type/count rejected')
            if name not in ('.','wheelhouse','deployment.json','llm') and not re.fullmatch(r'wheelhouse/[A-Za-z0-9_.+-]+\.whl',name):raise Error('unexpected archive path')
            total+=member.size
            if member.size<0 or total>256*1024*1024:raise Error('archive size rejected')
            path=destination/name
            if member.isdir():path.mkdir(mode=0o700,parents=True,exist_ok=True);continue
            path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            with tar.extractfile(member) as source, path.open('xb') as output:
                while True:
                    block=source.read(65536)
                    if not block:break
                    output.write(block)
            path.chmod(0o600)
    value=bundle(destination)
    expected=set(value['files'])|{'deployment.json'}
    actual={str(p.relative_to(destination)) for p in destination.rglob('*') if p.is_file()}
    if actual!=expected:raise Error('archive payload differs from deployment manifest')
    return value


def _pull(settings, tag=None, *, dry_run=False, get=fetch, run=subprocess.run):
    repo=settings.get('repo','')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):raise Error('invalid repository')
    for key in ('cache_dir','container_bundle_root','container_settings','upgrade_program'):
        if not isinstance(settings.get(key),str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+',settings[key]) or '..' in Path(settings[key]).parts:raise Error('invalid pull path: '+key)
    if not re.fullmatch(r'[A-Za-z0-9_-]+',settings.get('container','')):raise Error('invalid container')
    if tag is not None and not re.fullmatch(r'v[0-9][A-Za-z0-9.+_-]{0,79}',tag):raise Error('invalid tag')
    if dry_run:
        return {'dry_run':True,'repo':repo,'tag':tag,'would':'outbound release verification then configured read-only LXC upgrade','network_or_files_or_commands':False}
    api='https://api.github.com/repos/'+repo
    if tag is None:
        releases=json.loads(get(api+'/releases?per_page=10',1024*1024))
        releases=[x for x in releases if not x.get('draft') and x.get('published_at')]
        if not releases:raise Error('no published release')
        release=max(releases,key=lambda x:x['published_at']);tag=release['tag_name']
        if not re.fullmatch(r'v[0-9][A-Za-z0-9.+_-]{0,79}',tag):raise Error('unsupported published tag')
    else:release=json.loads(get(api+'/releases/tags/'+quote(tag,safe=''),1024*1024))
    if release.get('draft') or release.get('tag_name')!=tag:raise Error('release is draft or tag mismatch')
    tagobj=json.loads(get(api+'/git/ref/tags/'+quote(tag,safe=''),65536))['object']
    for _ in range(4):
        if tagobj['type']=='commit':break
        if tagobj['type']!='tag':raise Error('unsupported tag object')
        tagobj=json.loads(get(api+'/git/tags/'+tagobj['sha'],65536))['object']
    if tagobj['type']!='commit' or not re.fullmatch('[0-9a-f]{40}',tagobj['sha']):raise Error('tag commit unresolved')
    rows={name:checked_asset(release,name,repo,tag) for name in ('SHA256SUMS','release-manifest.json','deployment.tar.gz')}
    sums=checksums(get(rows['SHA256SUMS']['browser_download_url'],65536))
    manifest_raw=get(rows['release-manifest.json']['browser_download_url'],1024*1024)
    if sha(manifest_raw)!=sums.get('release-manifest.json'):raise Error('release manifest checksum mismatch')
    manifest=json.loads(manifest_raw)
    if manifest.get('commit')!=tagobj['sha'] or manifest.get('tag')!=tag:raise Error('release manifest/tag commit mismatch')
    expected=manifest.get('assets',{}).get('deployment.tar.gz',{})
    if expected.get('sha256')!=sums.get('deployment.tar.gz'):raise Error('bundle checksum manifests disagree')
    digest=sums['deployment.tar.gz']
    cache=Path(settings['cache_dir'])
    if cache.is_symlink():raise Error('cache must not be symlink')
    cache.mkdir(parents=True,mode=0o700,exist_ok=True)
    receipt_path=cache/(tag+'-'+tagobj['sha'][:12]+'.json')
    if receipt_path.exists():
        old=json.loads(receipt_path.read_text())
        if old.get('status')=='deployed' and old.get('bundle_sha256')==digest:
            return {'status':'already_deployed','tag':tag,'commit':tagobj['sha']}
        raise Error('previous attempt needs explicit operator reconciliation; no automatic retry loop')
    raw=get(rows['deployment.tar.gz']['browser_download_url'],128*1024*1024)
    if sha(raw)!=digest or len(raw)!=expected.get('bytes'):raise Error('deployment asset hash/size mismatch')
    owned=Path(tempfile.mkdtemp(prefix='release-',dir=cache));archive=owned/'deployment.tar.gz';archive.write_bytes(raw);archive.chmod(0o600)
    receipt={'status':'verifying','tag':tag,'commit':tagobj['sha'],'bundle_sha256':digest,'cache':str(owned),'scope':'read_only','generated_by':'Codex / gpt-6-astra'}
    json_write(receipt_path,receipt)
    try:
        value=unpack(archive,owned/'bundle')
        if value['tag']!=tag or value['commit']!=tagobj['sha'] or value['version']!=manifest.get('python_version'):raise Error('deployment payload release identity mismatch')
    except BaseException:
        receipt['status']='failed_validation';json_write(receipt_path,receipt);raise
    receipt['status']='prepared';json_write(receipt_path,receipt)
    prefix=settings.get('lxc_prefix',['sudo','-n','lxc'])
    container=settings['container'];remote=settings['container_bundle_root'].rstrip('/')+'/'+value['generation']
    def command(args,timeout=120):
        result=run(args,capture_output=True,text=True,timeout=timeout)
        if result.returncode:raise Error('pull deployment command failed; stderr_sha256='+sha(result.stderr.encode()))
        return result
    try:
        # Never replace a retained remote bundle or the installed deployer/config.
        command([*prefix,'exec',container,'--','mkdir','-p',settings['container_bundle_root']])
        command([*prefix,'exec',container,'--','test','!','-e',remote])
        local_bundle=owned/value['generation']
        (owned/'bundle').rename(local_bundle)
        command([*prefix,'file','push','-r',str(local_bundle),container+settings['container_bundle_root'].rstrip('/')+'/'])
        upgraded=command([*prefix,'exec',container,'--',settings.get('container_python','python3'),settings['upgrade_program'],'apply','--settings',settings['container_settings'],'--root','/','--bundle',remote],timeout=600)
        receipt.update(status='deployed',upgrade_stdout_sha256=sha(upgraded.stdout.encode()),remote_bundle=remote)
        json_write(receipt_path,receipt);emit('pull_deployed',tag=tag,commit=tagobj['sha'])
    except BaseException:
        receipt['status']='failed';json_write(receipt_path,receipt);raise
    return receipt



def pull(settings, tag=None, *, dry_run=False, get=fetch, run=subprocess.run):
    if dry_run:return _pull(settings,tag,dry_run=True,get=get,run=run)
    cache=Path(settings.get('cache_dir',''))
    if not cache.is_absolute() or cache.resolve()!=cache:raise Error('canonical absolute cache required')
    cache.mkdir(parents=True,mode=0o700,exist_ok=True)
    descriptor=os.open(cache/'.pull.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _pull(settings,tag,get=get,run=run)



def render_units(settings, settings_path):
    python=settings.get('host_python','/usr/bin/python3')
    program=settings.get('host_program','/opt/llmsvc-deploy/deploy/pull_release.py')
    for value in (python,program,str(settings_path)):
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+',value) or '..' in Path(value).parts:raise Error('unsafe systemd path')
    interval=settings.get('poll_interval_seconds',300)
    if type(interval) is not int or not 60<=interval<=86400:raise Error('invalid poll interval')
    service=f"""# Generated-By: Codex / gpt-6-astra
[Unit]
Description=Pull reviewed read-only LLM runtime releases
After=network-online.target
Wants=network-online.target
ConditionPathExists={settings_path}

[Service]
Type=oneshot
User=root
UMask=0077
ExecStart={python} {program} --settings {settings_path}
TimeoutStartSec=10min
StandardOutput=journal
StandardError=journal
"""
    timer=f"""# Generated-By: Codex / gpt-6-astra
[Unit]
Description=Poll LLM runtime releases through the single host deployer

[Timer]
OnBootSec=2min
OnUnitInactiveSec={interval}s
Unit=llmsvc-release-pull.service

[Install]
WantedBy=timers.target
"""
    return {'llmsvc-release-pull.service':service,'llmsvc-release-pull.timer':timer}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings',type=Path,required=True)
    parser.add_argument('--tag')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--render-systemd',type=Path,help='write units to a new review directory; does not install or start them')
    args=parser.parse_args(argv)
    try:
        settings=json.loads(args.settings.read_text())
        if args.render_systemd:
            units=render_units(settings,args.settings.resolve())
            if not args.dry_run:
                args.render_systemd.mkdir(mode=0o700)
                for name,content in units.items():
                    path=args.render_systemd/name;path.write_text(content);path.chmod(0o644)
            print(json.dumps({'dry_run':args.dry_run,'rendered_units':list(units),'installed':False}));return 0
        print(json.dumps(pull(settings,args.tag,dry_run=args.dry_run)));return 0
    except (Error,OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        emit('pull_error',error=str(exc));return 1


if __name__=='__main__':raise SystemExit(main())
