#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Bounded maintenance observations. Effects require a separately bound site plan.

This command never infers helper success, backend cleanup, or exclusion from
proxy exit. Unsupported effects fail closed while the core checkpoint persists.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time

MAX_REQUEST = 4 * 1024 * 1024
MAX_RESPONSE = 65536
PROPERTIES = ('Id', 'LoadState', 'ActiveState', 'SubState', 'MainPID',
              'ControlGroup', 'InvocationID', 'FragmentPath', 'KillMode', 'Delegate')


class ExecutorError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def run_bounded(argv, deadline, *, limit=MAX_RESPONSE, clock=time.monotonic):
    """Kill/reap only this command on timeout; external effects stay unknown."""
    if clock() >= deadline:
        raise ExecutorError('command_deadline_exceeded')
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline-clock()
                if remaining <= 0:
                    raise ExecutorError('command_deadline_exceeded')
                for key, _ in selector.select(min(remaining, .1)):
                    block = os.read(key.fd, 8192)
                    if not block:
                        selector.unregister(key.fileobj)
                    else:
                        buffers[key.data].extend(block)
                        if len(buffers[key.data]) > limit:
                            raise ExecutorError('command_output_limit')
            remaining = deadline-clock()
            if remaining <= 0:
                raise ExecutorError('command_deadline_exceeded')
            code = process.wait(timeout=remaining)
            if clock() >= deadline:
                raise ExecutorError('command_deadline_exceeded')
            if code:
                raise ExecutorError('command_failed')
            return buffers['stdout'].decode('utf-8')
    except (subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ExecutorError('command_deadline_or_encoding_error') from exc
    finally:
        if process.poll() is None:
            process.kill()
        # Reap the command itself, never a guessed process group/foreign helper.
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        process.stdout.close()
        process.stderr.close()


def validate_identity(value):
    if (not isinstance(value, dict) or set(value) != {'pid', 'start_ticks', 'scope_sha256'}
            or type(value['pid']) is not int or value['pid'] <= 0
            or not isinstance(value['start_ticks'], str)
            or not re.fullmatch(r'[0-9]{1,32}', value['start_ticks'])
            or not isinstance(value['scope_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', value['scope_sha256'])):
        raise ExecutorError('invalid_process_identity')
    return value


class ScopeInspector:
    """Read exact process identities and a pinned systemd cgroup scope."""
    def __init__(self, profile, *, proc_root=Path('/proc'),
                 cgroup_root=Path('/sys/fs/cgroup'), runner=run_bounded):
        self.profile = profile
        self.proc = Path(proc_root)
        self.cgroups = Path(cgroup_root)
        self.runner = runner
        unit = profile.get('unit')
        if not isinstance(unit, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', unit):
            raise ExecutorError('invalid_configured_unit')
        command = profile.get('systemctl', '/usr/bin/systemctl')
        if not isinstance(command, str) or not Path(command).is_absolute():
            raise ExecutorError('systemctl_must_be_absolute')
        self.command = command

    def process(self, pid):
        p = self.proc / str(pid)
        try:
            fields = (p/'stat').read_text().rsplit(') ', 1)[1].split()
            return {'pid': pid, 'start_ticks': fields[19], 'state': fields[0]}
        except FileNotFoundError:
            return None
        except (OSError, ValueError, IndexError) as exc:
            raise ExecutorError('process_identity_unreadable') from exc

    def compare(self, expected):
        validate_identity(expected)
        observed = self.process(expected['pid'])
        if observed is None:
            return {'old_identity_present': False, 'disposition': 'absent'}
        if observed['start_ticks'] != expected['start_ticks']:
            return {'old_identity_present': False, 'disposition': 'pid_reused'}
        # A zombie is still observable. Its parent must reap it before absence.
        return {'old_identity_present': True, 'disposition': 'same_identity',
                'state': observed['state']}

    def properties(self, deadline):
        raw = self.runner([self.command, 'show', self.profile['unit'], '--no-pager',
                           '--property='+','.join(PROPERTIES)], deadline)
        rows = [line.split('=', 1) for line in raw.splitlines() if '=' in line]
        props = dict(rows)
        if len(props) != len(rows) or any(name not in props for name in PROPERTIES):
            raise ExecutorError('incomplete_unit_properties')
        if props['Id'] != self.profile['unit'] or props['LoadState'] != 'loaded':
            raise ExecutorError('configured_unit_not_loaded')
        if not re.fullmatch(r'[0-9a-f]{32}', props['InvocationID']) or not int(props['InvocationID'], 16):
            raise ExecutorError('unit_invocation_unknown')
        return props

    def scope(self, props):
        cg = props['ControlGroup']
        if not cg.startswith('/') or '..' in Path(cg).parts or cg == '/':
            raise ExecutorError('unsafe_control_group')
        path = self.cgroups / cg.lstrip('/')
        if path.resolve() != path.absolute():
            raise ExecutorError('control_group_symlink')
        fragment = Path(props['FragmentPath'])
        if not fragment.is_absolute() or not fragment.is_file():
            raise ExecutorError('unit_fragment_unavailable')
        raw = fragment.read_bytes()
        if len(raw) > MAX_RESPONSE:
            raise ExecutorError('unit_fragment_too_large')
        fragment_hash = hashlib.sha256(raw).hexdigest()
        if fragment_hash != self.profile.get('fragment_sha256'):
            raise ExecutorError('unit_fragment_changed')
        boot = (self.proc/'sys/kernel/random/boot_id').read_text().strip()
        if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', boot):
            raise ExecutorError('boot_identity_unknown')
        value = {'boot_id': boot, 'unit': props['Id'], 'invocation_id': props['InvocationID'],
                 'control_group': cg, 'fragment_sha256': fragment_hash}
        return path, value, digest(value)

    def members(self, path):
        """Every descendant cgroup must be readable; an absent directory is unknown."""
        if not path.is_dir():
            raise ExecutorError('control_group_absent_not_settlement')
        paths = [path]
        def unreadable(exc):
            raise ExecutorError('control_group_members_unreadable') from exc
        for root, directories, _ in os.walk(path, followlinks=False, onerror=unreadable):
            for name in directories:
                child = Path(root)/name
                if child.is_symlink():
                    raise ExecutorError('control_group_symlink')
                paths.append(child)
                if len(paths) > 1024:
                    raise ExecutorError('control_group_count_limit')
        members = set()
        for group in paths:
            try:
                values = (group/'cgroup.procs').read_text().split()
                if any(not v.isdecimal() or int(v) <= 0 for v in values):
                    raise ExecutorError('invalid_control_group_members')
                members.update(map(int, values))
                if len(members) > 4096:
                    raise ExecutorError('control_group_member_limit')
            except OSError as exc:
                raise ExecutorError('control_group_members_unreadable') from exc
        return sorted(members)

    def inspect(self, deadline):
        props = self.properties(deadline)
        path, scope, scope_hash = self.scope(props)
        try:
            pid = int(props['MainPID'])
        except ValueError as exc:
            raise ExecutorError('main_pid_invalid') from exc
        if pid <= 0:
            raise ExecutorError('main_pid_absent')
        first = self.process(pid)
        actors = []
        for member in self.members(path):
            row = self.process(member)
            if row is None:
                raise ExecutorError('actor_changed_during_inspection')
            actors.append({'pid': member, 'start_ticks': row['start_ticks'],
                           'scope_sha256': scope_hash})
        second = self.process(pid)
        final_props = self.properties(deadline)
        if first is None or first != second or final_props != props or pid not in [a['pid'] for a in actors]:
            raise ExecutorError('instance_changed_during_inspection')
        return {'identity': {'pid': pid, 'start_ticks': first['start_ticks'],
                             'scope_sha256': scope_hash}, 'scope': scope, 'actors': actors,
                'external_helpers_confirmed': False, 'exclusion_confirmed': False}

    def observe_absence(self, expected, scope, actors, deadline):
        validate_identity(expected)
        if digest(scope) != expected['scope_sha256']:
            raise ExecutorError('scope_binding_mismatch')
        # Never select a different service or arbitrary cgroup using request data.
        if scope.get('unit') != self.profile['unit']:
            raise ExecutorError('scope_unit_mismatch')
        props = self.properties(deadline)
        path, current_scope, current_hash = self.scope(props)
        if current_scope != scope or current_hash != expected['scope_sha256']:
            raise ExecutorError('unit_scope_changed')
        if not isinstance(actors, list) or not 1 <= len(actors) <= 4096:
            raise ExecutorError('old_actor_inventory_missing')
        if expected not in actors:
            raise ExecutorError('old_main_missing_from_actor_inventory')
        details = []
        for actor in actors:
            validate_identity(actor)
            if actor['scope_sha256'] != expected['scope_sha256']:
                raise ExecutorError('actor_scope_mismatch')
            details.append({'identity': actor, **self.compare(actor)})
        first = self.members(path)
        second = self.members(path)
        final_props = self.properties(deadline)
        empty = not first and not second and props == final_props
        absent = all(not row['old_identity_present'] for row in details)
        return {'old_process_absent': not self.compare(expected)['old_identity_present'],
                'observed_actors_absent': absent, 'scope_empty': empty,
                'scope_processes': second, 'actor_observations': details,
                'settlement_confirmed': False, 'cleanup_confirmed': False,
                'backends_confirmed': False, 'exclusion_confirmed': False,
                'blockers': ['external_helper_attribution_and_exit_outcomes_required',
                             'independent_backend_and_exclusion_proofs_required']}


def handle(envelope, operation, inspector, *, dry_run=False, clock=time.monotonic):
    if not isinstance(envelope, dict) or envelope.get('operation') != operation:
        raise ExecutorError('operation_mismatch')
    request_id = envelope.get('request_id')
    body = {k:v for k,v in envelope.items() if k != 'request_id'}
    if request_id != digest(body):
        raise ExecutorError('request_digest_mismatch')
    context = envelope.get('context')
    seconds = envelope.get('timeout_seconds')
    if (not isinstance(context, dict) or type(seconds) not in (int,float)
            or not math.isfinite(seconds) or not 0 < seconds <= 900):
        raise ExecutorError('invalid_context_or_deadline')
    deadline = clock()+seconds
    result = {'request_id': request_id, 'transaction_id': context.get('transaction_id')}
    if operation == 'inspect':
        result.update(inspector.inspect(deadline))
    elif operation in ('observe_old', 'observe_candidate_absent'):
        key = 'old_identity' if operation == 'observe_old' else 'new_identity'
        result.update(inspector.observe_absence(context.get(key), context.get('observed_scope'),
                                               context.get('observed_actors'), deadline))
    elif dry_run:
        result.update(accepted=False, dry_run=True, planned_operation=operation,
                      blockers=['effects_require_bound_site_exclusion_and_helper_protocol'])
    else:
        raise ExecutorError('site_effect_or_proof_not_configured')
    if clock() >= deadline:
        raise ExecutorError('observation_deadline_exceeded')
    if len(canonical(result).encode()) > MAX_RESPONSE:
        raise ExecutorError('response_size_limit')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('operation')
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST+1)
        if len(raw) > MAX_REQUEST:
            raise ExecutorError('request_size_limit')
        profile_path = Path(args.profile)
        if profile_path.stat().st_size > MAX_RESPONSE:
            raise ExecutorError('profile_size_limit')
        profile = json.loads(profile_path.read_text())
        result = handle(json.loads(raw), args.operation, ScopeInspector(profile), dry_run=args.dry_run)
        print(canonical(result))
        return 0
    except (ExecutorError, OSError, ValueError, TypeError) as exc:
        # No raw config/context or subprocess output in public diagnostics.
        print(canonical({'error':str(exc) if isinstance(exc,ExecutorError) else 'invalid_input_or_observation'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
