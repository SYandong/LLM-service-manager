#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""First managed-start file/source executor; core alone creates/confirms leases.

All effects need a core submitted fence and an immutable private manifest. An
interrupted attempt is observed or rolled back; this module never repeats it.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import time

from deploy.maintenance_executor import ExecutorError, ScopeInspector, digest, stop_bound_process
from deploy.maintenance_native import (
    NativeAdapter, NativeHTTP, TAG_KEYS, file_bytes, identifier, private_create,
    private_directory, private_json, private_update,
)

KINDS = ('unit_fragment', 'native_config', 'launcher', 'launcher_config',
         'native_profile', 'attempt_environment')
EFFECTS = ('bootstrap_stage', 'bootstrap_activate', 'bootstrap_rollback')


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(path):
    p = Path(path)
    if p.parent.resolve(strict=True) != p.parent or not p.is_absolute():
        raise ExecutorError('bootstrap_noncanonical_target_parent')
    try:
        before = p.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ExecutorError('bootstrap_regular_unlinked_target_required')
    raw = file_bytes(p)
    after = p.lstat()
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ExecutorError('bootstrap_file_changed_during_read')
    return {'sha256': checksum(raw), 'mode': stat.S_IMODE(after.st_mode),
            'uid': after.st_uid, 'gid': after.st_gid}


def replace_owned(path, expected, data, wanted):
    """Compare then atomic replace; never edit a hardlink or follow a symlink."""
    path = Path(path)
    if checksum(data) != wanted['sha256']:
        raise ExecutorError('bootstrap_replacement_digest_changed')
    if snapshot(path) != expected:
        raise ExecutorError('bootstrap_destination_changed')
    temporary = path.with_name('.bootstrap-'+os.urandom(12).hex())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), wanted['mode'])
            os.fchown(stream.fileno(), wanted['uid'], wanted['gid'])
            stream.flush(); os.fsync(stream.fileno())
        if snapshot(path) != expected:
            raise ExecutorError('bootstrap_destination_changed')
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def unit_candidate(raw, environment_file):
    """Only change Restart inside Service and add the dedicated attempt file."""
    lines=raw.decode().splitlines(keepends=True); section=None; found=0; output=[]
    for line in lines:
        stripped=line.strip()
        if stripped.startswith('['):section=stripped
        if stripped=='EnvironmentFile='+environment_file:
            raise ExecutorError('bootstrap_attempt_environment_already_bound')
        if section=='[Service]' and stripped.startswith('Restart='):
            found+=1; output.extend(['Restart=no\n','EnvironmentFile='+environment_file+'\n'])
        else:output.append(line)
    if found!=1:raise ExecutorError('bootstrap_exact_restart_directive_required')
    return ''.join(output).encode()


class BootstrapAdapter:
    def __init__(self, profile, profile_path, *, proc_root=Path('/proc'),
                 cgroup_root=Path('/sys/fs/cgroup'), runner=None):
        self.profile_path = Path(profile_path)
        self.dispatch_hash = checksum(file_bytes(self.profile_path, 65536))
        self.manifest_path = Path(profile['manifest_path'])
        raw = file_bytes(self.manifest_path)
        self.manifest_hash = checksum(raw)
        if self.manifest_hash != profile['manifest_sha256']:
            raise ExecutorError('bootstrap_manifest_pin_changed')
        self.manifest = private_json(self.manifest_path)
        m = self.manifest
        if m.get('schema_version') != 1:
            raise ExecutorError('bootstrap_manifest_schema')
        self.state = private_directory(m['state_dir'])
        self.artifacts = Path(m['artifact_root'])
        if self.artifacts.resolve(strict=True) != self.artifacts:
            raise ExecutorError('bootstrap_artifact_root_not_canonical')
        self.default = identifier(m['default_model'])
        self.unit = m['default_unit']
        self.rows = m['files']
        if not isinstance(self.rows, dict) or set(self.rows) != set(KINDS):
            raise ExecutorError('bootstrap_exact_file_set_required')
        targets = set()
        for kind, row in self.rows.items():
            source, target = Path(row['source']), Path(row['target'])
            if (not source.is_absolute() or source.resolve(strict=True) != source
                    or not source.is_relative_to(self.artifacts)
                    or not target.is_absolute() or target.parent.resolve(strict=True) != target.parent
                    or str(target) in targets or source == target):
                raise ExecutorError('bootstrap_file_boundary')
            targets.add(str(target))
            after = row['after']
            if (not re.fullmatch('[0-9a-f]{64}', after['sha256'])
                    or after['mode'] not in (0o600, 0o644, 0o700, 0o755)
                    or after['uid'] != os.geteuid() or after['gid'] != os.getegid()):
                raise ExecutorError('bootstrap_target_metadata')
            if kind in ('native_profile', 'attempt_environment', 'launcher_config') and after['mode'] != 0o600:
                raise ExecutorError('bootstrap_private_target_mode')
        source = m['source_profile']
        if (self.rows['unit_fragment']['target'] != source['fragment_path']
                or self.rows['native_config']['target'] != source['native_config_path']
                or source['fragment_sha256'] != self.rows['unit_fragment']['before']['sha256']):
            raise ExecutorError('bootstrap_source_file_binding')
        self.proc, self.cgroups = Path(proc_root), Path(cgroup_root)
        options = {'proc_root': self.proc, 'cgroup_root': self.cgroups}
        if runner is not None: options['runner'] = runner
        self.options = options
        self.source = ScopeInspector(source, **options)
        self.runner = self.source.runner
        self.command = self.source.command
        self._validate_inputs()

    def _environment_guard(self):
        rows=self.manifest['native_environment_files']+[self.manifest['daemon_environment_file']]
        for row in rows:
            if checksum(file_bytes(row['path'],65536))!=row['sha256']:
                raise ExecutorError('bootstrap_environment_file_changed')

    def _validate_inputs(self):
        import yaml
        for row in self.rows.values():
            if checksum(file_bytes(row['source'])) != row['after']['sha256']:
                raise ExecutorError('bootstrap_artifact_changed')
        self.target_profile = json.loads(file_bytes(self.rows['native_profile']['source']))
        target = self.target_profile
        self._environment_guard()
        launch_config=json.loads(file_bytes(self.rows['launcher_config']['source']))
        if launch_config.get('systemd_run',{}).get('environment_file')!=self.manifest['daemon_environment_file']['path']:
            raise ExecutorError('bootstrap_daemon_environment_binding_required')
        if (target['unit'] != self.source.profile['unit']
                or target['fragment_path'] != self.rows['unit_fragment']['target']
                or target['fragment_sha256'] != self.rows['unit_fragment']['after']['sha256']
                or target['native_config_path'] != self.rows['native_config']['target']
                or target['launch_environment_file'] != self.rows['attempt_environment']['target']
                or target['native_binary_sha256'] != self.source.profile['native_binary_sha256']
                or target['listen_host'] != self.source.profile['listen_host']
                or target['listen_port'] != self.source.profile['listen_port']):
            raise ExecutorError('bootstrap_target_profile_binding')
        if self.default not in target['models'] or target['models'][self.default]['unit'] != self.unit:
            raise ExecutorError('bootstrap_default_unit_binding')
        cfg = yaml.safe_load(file_bytes(self.rows['native_config']['source']))
        preload = cfg.get('hooks', {}).get('on_startup', {}).get('preload', [])
        if preload != [self.default] or cfg.get('hooks', {}).get('on_startup', {}).get('profile'):
            raise ExecutorError('bootstrap_single_default_preload_required')
        program = Path(__file__).resolve().with_name('maintenance_native.py')
        if checksum(file_bytes(program)) != target['helper_program_sha256']:
            raise ExecutorError('bootstrap_helper_program_changed')
        NativeAdapter(target, Path(self.rows['native_profile']['source']), **self.options)
        if target.get('native_probe_origins') != self.source.profile.get('native_probe_origins'):
            raise ExecutorError('bootstrap_probe_contract_changed')
        for name, model in cfg['models'].items():
            expected = [target['helper_python'], '-B', str(program), 'helper', '--profile',
                        self.rows['native_profile']['target'], '--model', name, '--pid', '${PID}']
            if (shlex.split(model.get('cmdStop', '')) != expected
                    or shlex.split(model.get('cmd', '')) != target['models'][name]['process_argv']):
                raise ExecutorError('bootstrap_target_model_command_changed')
        self.target_config = cfg
        self.legacy_units = [row['unit'] for row in target['models'].values()]
        if (len(set(self.legacy_units)) != len(self.legacy_units)
                or any(not re.fullmatch(r'vllm-[A-Za-z0-9_.-]+\.service', u) for u in self.legacy_units)):
            raise ExecutorError('bootstrap_legacy_unit_bindings')
        fragment = file_bytes(self.rows['unit_fragment']['source']).decode()
        lines = [line.strip() for line in fragment.splitlines()]
        if (lines.count('Restart=no') != 1 or 'KillMode=control-group' not in lines
                or 'EnvironmentFile='+target['launch_environment_file'] not in lines
                or any(line.startswith(('ExecStartPre=', 'ExecStartPost=', 'ExecStop=', 'ExecStopPost=', 'ExecCondition='))
                       and line.split('=', 1)[1] for line in lines)):
            raise ExecutorError('bootstrap_target_unit_contract')
        if file_bytes(self.rows['attempt_environment']['source']) != b'':
            raise ExecutorError('bootstrap_attempt_environment_must_start_empty')

    def _context(self, context):
        value = identifier(context['bootstrap_id'])
        if value in ('.','..'):raise ExecutorError('bootstrap_identifier_path_component')
        if (context.get('transaction_id') != value or context.get('manifest_sha256') != self.manifest_hash
                or context.get('default_model') != self.default or context.get('default_unit') != self.unit):
            raise ExecutorError('bootstrap_context_binding')
        if checksum(file_bytes(self.manifest_path)) != self.manifest_hash:
            raise ExecutorError('bootstrap_manifest_changed')
        if checksum(file_bytes(self.profile_path, 65536)) != self.dispatch_hash:
            raise ExecutorError('bootstrap_dispatch_profile_changed')
        self._environment_guard()
        root=self.state/value
        if root.is_symlink():raise ExecutorError('bootstrap_state_directory_symlink')
        if root.exists():private_directory(root)
        return root

    def _effect(self, operation, context):
        mark = context.get('effects', {}).get(operation, {})
        if mark.get('submitted') is not True or mark.get('acknowledged') is not False:
            raise ExecutorError('bootstrap_durable_submission_required')

    def _show(self, unit, deadline):
        keys = ('Id', 'LoadState', 'ActiveState', 'SubState', 'MainPID', 'ControlGroup',
                'InvocationID', 'FragmentPath', 'DropInPaths', 'Restart', 'KillMode', 'Job', 'NeedDaemonReload')
        raw = self.runner([self.command, 'show', unit, '--property='+','.join(keys)], deadline)
        result = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
        if any(k not in result for k in keys) or result['Id'] != unit or not result['MainPID'].isdecimal():
            raise ExecutorError('bootstrap_unit_properties_incomplete')
        return result

    def _unit_absent(self, unit, deadline):
        p = self._show(unit, deadline)
        return (p['LoadState'] in ('loaded', 'not-found') and p['ActiveState'] in ('inactive', 'failed')
                and p['MainPID'] == '0' and not p['ControlGroup'] and p['Job'] in ('', '0'))

    def _legacy_absent(self, deadline):
        return all(self._unit_absent(unit, deadline) for unit in self.legacy_units)

    def _capture(self, deadline, *, staged_fragment=False):
        import yaml
        p = dict(self.source.profile)
        if staged_fragment: p['fragment_sha256'] = self.rows['unit_fragment']['after']['sha256']
        inspector = ScopeInspector(p, **self.options)
        before = inspector.inspect(deadline)
        props = self._show(p['unit'], deadline)
        if props['DropInPaths'] or props['KillMode'] != 'control-group' or props['NeedDaemonReload'] != 'no':
            raise ExecutorError('bootstrap_untracked_source_unit')
        if staged_fragment and props['Restart'] != 'no':
            raise ExecutorError('bootstrap_source_auto_restart')
        if not staged_fragment:
            fragment=file_bytes(p['fragment_path'],65536)
            expected=unit_candidate(fragment,self.target_profile['launch_environment_file'])
            if checksum(expected)!=self.rows['unit_fragment']['after']['sha256']:
                raise ExecutorError('bootstrap_unit_delta_not_narrow')
            actual=[line.strip().split('=',1)[1] for line in fragment.decode().splitlines()
                    if line.strip().startswith('EnvironmentFile=')]
            if actual!=[row['path'] for row in self.manifest['native_environment_files']]:
                raise ExecutorError('bootstrap_native_environment_binding_required')
        inspector.config = Path(p['native_config_path'])
        NativeAdapter.native_image(inspector, before['identity']['pid'])
        raw = file_bytes(inspector.config)
        if checksum(raw) != self.rows['native_config']['before']['sha256']:
            raise ExecutorError('bootstrap_source_config_changed')
        cfg = yaml.safe_load(raw)
        if cfg.get('hooks', {}).get('on_startup', {}).get('preload', []) != [self.default]:
            raise ExecutorError('bootstrap_original_default_preload_changed')
        if (cfg.get('hooks', {}).get('on_startup', {}).get('profile')
                or set(cfg.get('models', {})) != set(self.target_profile['models'])):
            raise ExecutorError('bootstrap_original_model_topology_changed')
        if {k:v for k,v in cfg.items() if k != 'models'} != {k:v for k,v in self.target_config.items() if k != 'models'}:
            raise ExecutorError('bootstrap_unrelated_native_setting_changed')
        for name, model in cfg['models'].items():
            keep = lambda row: {k:v for k,v in row.items() if k not in ('cmd', 'cmdStop', 'proxy')}
            if keep(model) != keep(self.target_config['models'][name]):
                raise ExecutorError('bootstrap_alias_or_model_setting_changed')
        if not self._legacy_absent(deadline):
            raise ExecutorError('bootstrap_legacy_backend_present')
        origins = p.get('native_probe_origins', [p['native_origin']])
        if p['listen_host'] == '' and 'native_probe_origins' not in p:
            raise ExecutorError('bootstrap_wildcard_probe_contract_required')
        # Every child must be one of the pinned read-only auxiliaries. A wrapper
        # or untracked stop helper prevents this initial migration.
        for actor in before['actors']:
            if actor == before['identity']: continue
            path = inspector.proc/str(actor['pid'])
            args = [a.decode() for a in file_bytes(path/'cmdline', 65536).split(b'\0') if a]
            parent = (path/'stat').read_text().rsplit(') ', 1)[1].split()[1]
            with (path/'exe').open('rb') as stream:
                image = stream.read(256*1024*1024+1)
            if len(image)>256*1024*1024:raise ExecutorError('bootstrap_auxiliary_image_limit')
            matched = any(args == row['argv'] and checksum(image) == row['sha256']
                          for row in p.get('source_auxiliaries', []))
            if parent != str(before['identity']['pid']) or not matched:
                raise ExecutorError('bootstrap_legacy_actor_present')
        for origin in origins:
            if not NativeAdapter.listener_owned(inspector, origin, before['scope']['control_group'], deadline):
                raise ExecutorError('bootstrap_source_listener_unowned')
            observed = NativeHTTP(origin).snapshot(deadline)
            if (set(observed.states) != set(cfg['models']) or observed.requests
                    or any(state != 'stopped' for state in observed.states.values())):
                raise ExecutorError('bootstrap_source_busy_or_model_present')
        final = inspector.inspect(deadline)
        NativeAdapter.native_image(inspector, before['identity']['pid'])
        if (final['identity'] != before['identity'] or final['actors'] != before['actors']
                or file_bytes(inspector.config) != raw or not self._legacy_absent(deadline)
                or any(not NativeAdapter.listener_owned(inspector, origin, before['scope']['control_group'], deadline)
                       for origin in origins)):
            raise ExecutorError('bootstrap_source_changed')
        return {**before, 'in_flight': 0, 'legacy_backends_absent': True,
                'default_preload_preserved': True}

    def _base(self):
        return {'manifest_sha256': self.manifest_hash, 'default_model': self.default,
                'default_unit': self.unit, 'observed_at': time.monotonic()}

    def _files_match(self, side, *, ignore_environment=False):
        return all(snapshot(row['target']) == row[side] for kind, row in self.rows.items()
                   if not (ignore_environment and kind == 'attempt_environment'))

    def preflight(self, context, deadline):
        if not self._files_match('before'):
            raise ExecutorError('bootstrap_baseline_files_changed')
        self._validate_inputs()
        return {**self._base(), **self._capture(deadline), 'ready': True,
                'launcher_sha256': self.rows['launcher']['after']['sha256'],
                'launcher_config_sha256': self.rows['launcher_config']['after']['sha256'],
                'source_config_sha256': self.rows['native_config']['before']['sha256']}

    def _record(self, root, context):
        value = private_json(root/'record.json')
        if value['bootstrap_id'] != context['bootstrap_id'] or value['manifest_sha256'] != self.manifest_hash:
            raise ExecutorError('bootstrap_record_binding')
        return value

    @contextlib.contextmanager
    def _lock(self, root, *, exclusive):
        fd = os.open(root/'lock', os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            yield
        finally: os.close(fd)

    def _absent(self, record, deadline):
        source = self._show(self.source.profile['unit'], deadline)
        if (source['LoadState'] != 'loaded' or source['DropInPaths']
                or source['FragmentPath'] != self.source.profile['fragment_path']
                or source['KillMode'] != 'control-group' or source['NeedDaemonReload'] != 'no'
                or source['MainPID'] != '0' or source['ActiveState'] not in ('inactive', 'failed')
                or source['ControlGroup'] or source['Job'] not in ('', '0')):
            return False
        old = record['preflight']
        if any(self.source.compare(actor)['old_identity_present'] for actor in old['actors']): return False
        group = self.cgroups/old['scope']['control_group'].lstrip('/')
        return not group.exists() or not self.source.members(group)

    def _install(self, root, record, kind):
        row = self.rows[kind]
        record = private_update(root/'record.json', record, {'pending_file': kind})
        replace_owned(row['target'], row['before'], file_bytes(row['source']), row['after'])
        return private_update(root/'record.json', record,
                              {'pending_file': None, 'installed': record['installed']+[kind]})

    def stage(self, context, root, deadline):
        before = self.preflight(context, deadline)
        if context.get('source_identity') != before['identity']:
            raise ExecutorError('bootstrap_preflight_identity_changed')
        # Creation is exclusive: an interrupted or completed operation is never
        # replayed, even when core did not receive its reply.
        root.mkdir(mode=0o700)
        parent = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(parent)
        finally: os.close(parent)
        fd = os.open(root/'lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.close(fd)
        with self._lock(root, exclusive=True):
            record = {'bootstrap_id': context['bootstrap_id'], 'manifest_sha256': self.manifest_hash,
                      'preflight': before, 'installed': [], 'pending_file': None,
                      'stop_submitted': False, 'source_absent_observed': False,
                      'activation_submitted': False, 'rolled_back': False}
            private_create(root/'record.json', record)
            for kind, row in self.rows.items():
                if row['before'] is not None:
                    data = file_bytes(row['target'])
                    if checksum(data) != row['before']['sha256']: raise ExecutorError('bootstrap_backup_changed')
                    fd = os.open(root/(kind+'.backup'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as stream: stream.write(data); stream.flush(); os.fsync(stream.fileno())
            record = self._install(root, record, 'unit_fragment')
            record = private_update(root/'record.json', record, {'daemon_reload_submitted': True})
            self.runner([self.command, 'daemon-reload'], deadline)
            fresh = self._capture(deadline, staged_fragment=True)
            if any(fresh['identity'][key] != before['identity'][key] for key in ('pid', 'start_ticks')):
                raise ExecutorError('bootstrap_source_replaced_before_stop')
            record = private_update(root/'record.json', record, {'stop_submitted': True,
                                                                 'stop_identity': fresh['identity']})
            owner = self
            class Bound:
                def inspect(self, end): return owner._capture(end, staged_fragment=True)
            stop_bound_process(fresh['identity'], Bound(), deadline)
            while not self._absent(record, deadline):
                if time.monotonic() >= deadline: raise ExecutorError('bootstrap_old_source_exit_unknown')
                time.sleep(min(.05, max(0, deadline-time.monotonic())))
            if not self._legacy_absent(deadline): raise ExecutorError('bootstrap_late_legacy_backend')
            record = private_update(root/'record.json', record, {'source_absent_observed': True})
            for kind in KINDS:
                if kind != 'unit_fragment': record = self._install(root, record, kind)
            private_update(root/'record.json', record, {'staged': True})
            return self._observe(root, context, deadline)

    def _native(self):
        path = Path(self.rows['native_profile']['target'])
        return NativeAdapter(private_json(path), path, **self.options)

    def _account(self, context, adapter, deadline):
        account = context.get('account')
        if (not isinstance(account, dict) or account.get('model') != self.default
                or account.get('unit') != self.unit or account.get('status') != 'confirmed'):
            raise ExecutorError('bootstrap_confirmed_default_required')
        binding = adapter.backend(self.default, deadline)
        if binding['lease_id'] != account.get('lease_id') or binding['gpu'] != account.get('gpu'):
            raise ExecutorError('bootstrap_default_account_mismatch')
        if any(not self._unit_absent(unit, deadline) for unit in self.legacy_units if unit != self.unit):
            raise ExecutorError('bootstrap_unexpected_backend')
        if account.get('invocation_id') is not None and account['invocation_id'] != binding['invocation_id']:
            raise ExecutorError('bootstrap_default_invocation_changed')
        return account, binding

    def activate(self, context, root, deadline):
        with self._lock(root, exclusive=True):
            record = self._record(root, context)
            if (not record['source_absent_observed']
                    or record['activation_submitted'] or record['rolled_back']
                    or not self._files_match('after') or not self._absent(record, deadline)):
                raise ExecutorError('bootstrap_activation_state_unknown')
            adapter = self._native(); self._account(context, adapter, deadline)
            adapter.validate(str(adapter.config), self.rows['native_config']['after']['sha256'], deadline)
            if not adapter._address_available(): raise ExecutorError('bootstrap_listener_busy')
            record = private_update(root/'record.json', record, {'activation_submitted': True})
            tags = (context['bootstrap_id'], self.manifest_hash, 'bootstrap')
            data = ''.join(k+'='+v+'\n' for k, v in zip(TAG_KEYS, tags)).encode()
            row = self.rows['attempt_environment']
            wanted = dict(row['after'], sha256=checksum(data))
            replace_owned(row['target'], row['after'], data, wanted)
            record = private_update(root/'record.json', record,
                                    {'activation_environment': wanted, 'start_submitted': True})
            self.runner([self.command, 'start', adapter.profile['unit']], deadline)
            while time.monotonic() < deadline:
                try:
                    value = self._observe(root, context, deadline)
                    if value.get('active_ready'): return value
                except (OSError, ValueError, ExecutorError): pass
                time.sleep(min(.05, max(0, deadline-time.monotonic())))
            raise ExecutorError('bootstrap_activation_unknown_observe_only')

    def _rollback_proof(self, record, context, deadline):
        if (not record.get('rollback_submitted') or context.get('launch_submitted') is not False
                or context.get('account') is not None or not self._files_match('before')
                or not self._legacy_absent(deadline)):
            return False
        if self._absent(record,deadline):return True
        if record['stop_submitted']:return False
        current=self._capture(deadline)
        return all(current['identity'][k]==record['preflight']['identity'][k] for k in ('pid','start_ticks'))

    def _observe(self, root, context, deadline):
        record = self._record(root, context)
        absent = self._absent(record, deadline)
        staged = self._files_match('after', ignore_environment=record['activation_submitted'])
        if record['activation_submitted']:
            staged = staged and snapshot(self.rows['attempt_environment']['target']) == record.get('activation_environment')
        value = {**self._base(), 'source_absent': absent,
                 'helpers_settled': absent and record['source_absent_observed'],
                 'staged': staged and record['source_absent_observed'] and not record['rolled_back'], 'in_flight': None,
                 'legacy_backends_absent': self._legacy_absent(deadline),
                 'active_ready': False, 'default_confirmed': False,
                 'rolled_back': self._rollback_proof(record,context,deadline)}
        for kind, key in [('launcher', 'launcher_sha256'), ('launcher_config', 'launcher_config_sha256'),
                          ('native_config', 'source_config_sha256')]:
            actual = snapshot(self.rows[kind]['target'])
            value[key] = actual['sha256'] if actual else None
        if not absent and record.get('start_submitted') and staged:
            adapter = self._native(); account, binding = self._account(context, adapter, deadline)
            observed = adapter.inspect_native({'current_accounts': [[account, self.unit]]}, deadline)
            if all(observed['identity'][k] == record['preflight']['identity'][k] for k in ('pid', 'start_ticks')):
                raise ExecutorError('bootstrap_new_source_identity_required')
            if snapshot(adapter.envfile) != record.get('activation_environment'):
                raise ExecutorError('bootstrap_attempt_environment_changed')
            env = {}
            for item in file_bytes(adapter.proc/str(observed['identity']['pid'])/'environ', 1048576).split(b'\0'):
                if b'=' in item:
                    key, val = item.split(b'=', 1)
                    if key.decode(errors='replace') in TAG_KEYS: env[key.decode()] = val.decode()
            if env != dict(zip(TAG_KEYS, (context['bootstrap_id'], self.manifest_hash, 'bootstrap'))):
                raise ExecutorError('bootstrap_source_attempt_unbound')
            value.update(active_ready=True, default_confirmed=True, default_binding=binding, identity=observed['identity'],
                         scope=observed['scope'], actors=observed['actors'],
                         in_flight=observed['in_flight'], helpers_settled=record['source_absent_observed'])
        if private_json(root/'record.json') != record:
            raise ExecutorError('bootstrap_record_changed_during_observation')
        value['observed_at'] = time.monotonic()
        return value

    def rollback(self, context, root, deadline):
        with self._lock(root, exclusive=True):
            record = self._record(root, context)
            absent = self._absent(record, deadline)
            if (context.get('launch_submitted') is not False or context.get('account') is not None
                    or record['activation_submitted'] or not self._legacy_absent(deadline)
                    or record['rolled_back'] or record.get('rollback_submitted')):
                raise ExecutorError('bootstrap_rollback_would_cross_live_or_unknown_start')
            if not absent:
                # A failure before stop submission may have installed only the
                # fragment. Restore it while retaining the exact old process;
                # no restart or model stop is necessary or permitted here.
                if (record['stop_submitted'] or any(snapshot(row['target']) != row['before']
                        for kind, row in self.rows.items() if kind != 'unit_fragment')):
                    raise ExecutorError('bootstrap_rollback_source_outcome_unknown')
                changed = snapshot(self.rows['unit_fragment']['target']) == self.rows['unit_fragment']['after']
                current = self._capture(deadline, staged_fragment=changed)
                if any(current['identity'][k] != record['preflight']['identity'][k] for k in ('pid','start_ticks')):
                    raise ExecutorError('bootstrap_rollback_original_instance_changed')
            # Refuse known foreign files or corrupt backups before restoring any
            # target. Per-file checks still guard races during the transaction.
            for kind, row in self.rows.items():
                if snapshot(row['target']) not in (row['before'], row['after']):
                    raise ExecutorError('bootstrap_rollback_foreign_file')
                if row['before'] is not None and checksum(file_bytes(root/(kind+'.backup'))) != row['before']['sha256']:
                    raise ExecutorError('bootstrap_backup_corrupt')
            record = private_update(root/'record.json', record, {'rollback_submitted': True})
            for kind in reversed(KINDS):
                row = self.rows[kind]; actual = snapshot(row['target'])
                if actual == row['before']: continue
                if actual != row['after']: raise ExecutorError('bootstrap_rollback_foreign_file')
                record = private_update(root/'record.json', record, {'rollback_pending_file': kind})
                if row['before'] is None:
                    if snapshot(row['target']) != row['after']: raise ExecutorError('bootstrap_rollback_file_changed')
                    Path(row['target']).unlink()
                else:
                    data = file_bytes(root/(kind+'.backup'))
                    if checksum(data) != row['before']['sha256']: raise ExecutorError('bootstrap_backup_corrupt')
                    replace_owned(row['target'], row['after'], data, row['before'])
                record = private_update(root/'record.json', record, {'rollback_pending_file': None})
            self.runner([self.command, 'daemon-reload'], deadline)
            if absent:
                if not self._absent(record, deadline): raise ExecutorError('bootstrap_rollback_source_not_absent')
            else:
                restored = self._capture(deadline)
                if any(restored['identity'][k] != record['preflight']['identity'][k] for k in ('pid','start_ticks')):
                    raise ExecutorError('bootstrap_rollback_original_instance_changed')
            private_update(root/'record.json', record, {'rolled_back': True})
            return {**self._base(), 'rolled_back': True, 'source_absent': absent,
                    'original_source_retained': not absent, 'old_source_restarted': False, 'ledger_restored': False}

    def operation(self, operation, context, deadline, dry_run=False):
        root = self._context(context)
        if time.monotonic() >= deadline: raise ExecutorError('bootstrap_deadline')
        if operation not in (*EFFECTS, 'bootstrap_preflight', 'bootstrap_observe'):
            raise ExecutorError('unsupported_bootstrap_operation')
        if dry_run:
            return {**self._base(), 'dry_run': True, 'accepted': False,
                    'planned_operation': operation}  # No commands, locks or files.
        if operation == 'bootstrap_preflight': return self.preflight(context, deadline)
        if operation == 'bootstrap_observe':
            with self._lock(root, exclusive=False): return self._observe(root, context, deadline)
        self._effect(operation, context)
        if operation == 'bootstrap_stage': value = self.stage(context, root, deadline)
        elif operation == 'bootstrap_activate': value = self.activate(context, root, deadline)
        else: value = self.rollback(context, root, deadline)
        return {**value, 'accepted': True}  # Timely ACK; core still checks every proof field.
