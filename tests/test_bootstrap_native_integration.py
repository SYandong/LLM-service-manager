# Generated-By: Codex / gpt-6-astra
"""Actual core/ops wire checks; source/systemd observations use the owner fixture."""
import sys
import threading
import time
from pathlib import Path

import pytest

from deploy.maintenance_executor import ExecutorError, digest, handle
from llmsvc.bootstrap import BootstrapController
from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import Scheduler
from llmsvc.store import IntentStore
from test_deploy_bootstrap_native import site


@pytest.fixture
def caller(site,tmp_path):
    rows=site.rows
    spec={'model':'m','util':.4,'command':[sys.executable,'--port','54322','--gpu-memory-utilization','.4'],
        'launcher_path':rows['launcher']['source'],'launcher_sha256':rows['launcher']['after']['sha256'],
        'launcher_config_path':rows['launcher_config']['source'],'launcher_config_sha256':rows['launcher_config']['after']['sha256'],
        'migration_command':[sys.executable,'fixture'],'manifest_sha256':site.profile['manifest_sha256'],
        'base_config_sha256':rows['native_config']['before']['sha256'],'target_config_sha256':rows['native_config']['after']['sha256'],
        'timeout_seconds':5}
    config=SchedulerConfig('127.0.0.1',8011,read_only=False,state_db_path=str(tmp_path/'core.sqlite'),
        bootstrap_enabled=True,bootstrap=spec,model_actions_enabled=True,placement_enabled=True,
        catalog_enabled=True,catalog_mode='maintenance',collectors={'swap_url':site.manifest['source_profile']['native_origin'],
        'models':{'m':{'unit':'vllm-m.service','is_default':True,'weights_gb':10,'port':54322}}},
        registry={'config_path':rows['native_config']['target'],'shared_roots':[str(tmp_path)],'daemon_port_range':[54322,54323]})
    store=IntentStore(config.state_db_path,action_lock=threading.RLock());scheduler=Scheduler(config,store=store)
    scheduler.placement=object()  # These two tests never enter placement or claim an account.
    class Backend:
        def request(self,operation,context,*,deadline):
            envelope={'operation':operation,'context':context,'timeout_seconds':deadline-time.monotonic()}
            envelope['request_id']=digest(envelope)
            return handle(envelope,operation,site.adapter)
    controller=BootstrapController(scheduler,backend=Backend());controller.pending_id='1'*32;controller.deadline=time.monotonic()+5
    try:yield controller
    finally:store.close()


def test_actual_preflight_return_is_bound_to_the_core_source(caller,site):
    result=caller._request('bootstrap_preflight')
    assert result['ready'] is True and result['source_origin']==caller.source_origin
    assert caller.scheduler.store.bootstrap_checkpoint() is None
    assert not (site.adapter.state/caller.pending_id).exists()


def test_adapter_rejects_a_different_telemetry_origin_before_commands(caller,site):
    context=caller._context();context['source_origin']='http://127.0.0.1:1'
    before=list(site.calls)
    with pytest.raises(ExecutorError,match='origin'):
        site.adapter.operation('bootstrap_preflight',context,time.monotonic()+5)
    assert site.calls==before


# The following integration uses real core HTTP placement/launcher/health and
# the actual file/source adapter. Native/systemd observations remain the owned
# site fixture; backend PID/environment observations come from the real child.
from dataclasses import replace
import json
import os
from llmsvc.actions import ManagedModelTransport
from llmsvc.leases import PlacementController
from test_bootstrap_execution import bootstrap_service


@pytest.fixture
def combined(bootstrap_service,site):
    c=bootstrap_service;controller=c.c
    model='m';unit='vllm-m.service'
    original_meta=c.s.config.collectors['models']['default']
    metadata={**original_meta,'unit':unit}
    launcher_data=Path(c.spec['launcher_path']).read_bytes()
    launcher_cfg=json.loads(Path(c.spec['launcher_config_path']).read_bytes())
    launcher_cfg['systemd_run']['environment_file']=site.manifest['daemon_environment_file']['path']
    target_profile=json.loads(Path(site.rows['native_profile']['source']).read_bytes())
    target_profile['models']['m']['backend_origin']=metadata['daemon_url']
    payloads={'launcher':launcher_data,'launcher_config':json.dumps(launcher_cfg).encode(),
              'native_profile':json.dumps(target_profile).encode()}
    import hashlib
    for kind,raw in payloads.items():
        Path(site.rows[kind]['source']).write_bytes(raw)
        site.rows[kind]['after']['sha256']=hashlib.sha256(raw).hexdigest()
    site.manifest['files']=site.rows
    site.manifest_path.write_text(json.dumps(site.manifest))
    site.profile['manifest_sha256']=hashlib.sha256(site.manifest_path.read_bytes()).hexdigest()
    site.profile_path.write_text(json.dumps(site.profile))
    old_runner=site.adapter.runner
    original_probe=c.s.placement.probe
    def runner(argv,deadline):
        process=c.world['unit'];active=process is not None and process.poll() is None
        site.backend_live[0]=active
        raw=old_runner(argv,deadline)
        if len(argv)>2 and argv[1]=='show' and argv[2]==unit and active:
            observed=original_probe(model,deadline=deadline)
            values={k:v for k,v in (line.split('=',1) for line in raw.splitlines())}
            values.update(MainPID=str(process.pid),InvocationID=observed.invocation_id,
                          Environment='LLMSVC_LEASE_ID='+observed.lease_id+' CUDA_VISIBLE_DEVICES=0')
            directory=site.proc/str(process.pid);directory.mkdir(exist_ok=True)
            for name in ('stat','cmdline','environ'):
                (directory/name).write_bytes(Path('/proc/'+str(process.pid)+'/'+name).read_bytes())
            raw='\n'.join(k+'='+v for k,v in values.items())
        return raw
    from deploy.bootstrap_native import BootstrapAdapter
    adapter=BootstrapAdapter(site.profile,site.profile_path,proc_root=site.proc,cgroup_root=site.group.parent,runner=runner)
    spec={**c.spec,'model':model,'launcher_path':site.rows['launcher']['source'],
          'launcher_sha256':site.rows['launcher']['after']['sha256'],
          'launcher_config_path':site.rows['launcher_config']['source'],
          'launcher_config_sha256':site.rows['launcher_config']['after']['sha256'],
          'manifest_sha256':site.profile['manifest_sha256'],
          'base_config_sha256':site.rows['native_config']['before']['sha256'],
          'target_config_sha256':site.rows['native_config']['after']['sha256']}
    c.s.config=replace(c.s.config,bootstrap=spec,
        collectors={'swap_url':site.manifest['source_profile']['native_origin'],'models':{model:metadata}},
        registry={**c.s.config.registry,'config_path':site.rows['native_config']['target']})
    collect=c.s.collect
    def collect_default():
        snapshot=collect()
        return replace(snapshot,models=tuple(replace(m,name=model,unit=unit) for m in snapshot.models),
                       activity=tuple(replace(a,model=model) for a in snapshot.activity))
    c.s.collect=collect_default
    transport=ManagedModelTransport(swap_url=c.s.config.collectors['swap_url'],models={model:metadata},systemctl='unused')
    c.s.placement=PlacementController(c.s,transport,probe=original_probe)
    class Backend:
        def request(self,operation,context,*,deadline):
            envelope={'operation':operation,'context':context,'timeout_seconds':deadline-time.monotonic()}
            envelope['request_id']=digest(envelope)
            return handle(envelope,operation,adapter)
    # Reinitialize the same fixture instance so its captured launcher harness
    # still uses the actual controller and the newly pinned manifest inputs.
    BootstrapController.__init__(controller,c.s,backend=Backend())
    c.native=adapter;c.native_site=site
    return c


def test_actual_core_and_native_adapter_complete_real_default_account(combined):
    c=combined
    result=c.c.run()
    assert result['stage']=='complete'
    record=c.store.bootstrap_checkpoint();lease,unit=c.store.lease(record['lease_id'])
    assert lease.model=='m' and lease.status=='confirmed' and lease.budget_gb==40
    assert unit=='vllm-m.service' and c.world['unit'].poll() is None
    assert record['migration']['activated']['default_binding']['lease_id']==lease.lease_id
    assert c.native._files_match('after',ignore_environment=True)
    assert not c.store.bootstrap_pending()
