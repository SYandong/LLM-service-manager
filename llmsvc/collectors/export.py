# Generated-By: Codex / gpt-6-astra
"""Export sanitized replay inputs; expected policy actions are authored separately."""

import argparse
import json
import math
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

MAX_BYTES = 4 * 1024 * 1024
KINDS = {'sleep', 'stop', 'wake', 'place', 'start'}


class Sanitizer:
    def __init__(self):
        self.labels = {}

    def label(self, kind, value):
        if value is None:
            return None
        labels = self.labels.setdefault(kind, {})
        key = str(value)
        return labels.setdefault(key, '{}-{}'.format(kind, len(labels) + 1))

    @staticmethod
    def fields(row, numbers=(), booleans=()):
        if not isinstance(row, dict):
            raise ValueError('snapshot records must be objects')
        result = {}
        for key in numbers:
            value = row.get(key)
            result[key] = value if type(value) in (int, float) and math.isfinite(value) else None
        for key in booleans:
            value = row.get(key)
            result[key] = value if type(value) is bool else None
        return result

    def snapshot(self, source):
        if not isinstance(source, dict) or source.get('schema_version') != 1:
            raise ValueError('expected a schema_version=1 state snapshot')
        result = self.fields(source, ('sampled_at',), ('read_only',))
        result['schema_version'] = 1
        for name in ('gpus', 'models', 'activity', 'pins', 'reserves', 'leases', 'blocked_by'):
            if not isinstance(source.get(name, []), (list, tuple)):
                raise ValueError('snapshot collections must be arrays')
        result['models'] = []
        for model in source.get('models', []):
            row = self.fields(model, ('gpu', 'util', 'budget_gb', 'weights_gb', 'resident_gb', 'port', 'cold_start_seconds'),
                              ('unit_active', 'health_ok', 'is_sleeping', 'is_default'))
            row['name'] = self.label('model', model.get('name'))
            row['unit'] = 'vllm-{}.service'.format(row['name']) if model.get('unit') else None
            row['state'] = model.get('state') if model.get('state') in ('awake', 'sleeping', 'stopped', 'unknown') else 'unknown'
            row['swap_state'] = model.get('swap_state') if model.get('swap_state') in ('ready', 'starting', 'stopping', 'stopped') else None
            result['models'].append(row)
        result['gpus'] = []
        for gpu in source.get('gpus', []):
            row = self.fields(gpu, ('index', 'total_gb', 'used_gb', 'free_gb', 'managed_gb', 'external_gb', 'utilization_percent'))
            row['uuid'] = self.label('gpu', gpu.get('uuid'))
            row['external_processes'] = []
            for process in gpu.get('external_processes', []):
                item = self.fields(process, ('used_gb',))
                pid = self.label('pid', process.get('pid'))
                item['pid'] = int(pid.split('-')[-1]) if pid else None
                item['name'] = self.label('process', process.get('name'))
                item['model'] = self.label('model', process.get('model'))
                item['user'] = self.label('source', process.get('user'))
                row['external_processes'].append(item)
            result['gpus'].append(row)
        result['activity'] = []
        for activity in source.get('activity', []):
            row = self.fields(activity, ('last_request_at', 'requests_last_hour', 'requests_last_10m', 'in_flight'))
            row['model'] = self.label('model', activity.get('model'))
            row['by'] = [self.label('source', value) for value in activity.get('by', [])]
            result['activity'].append(row)
        for name, numbers in (
            ('pins', ('until',)), ('reserves', ('gpu', 'size_gb', 'until')),
            ('leases', ('gpu', 'util', 'expires_at', 'budget_gb')),
            ('blocked_by', ('gpu', 'in_flight')),
        ):
            result[name] = []
            for value in source.get(name, []):
                row = self.fields(value, numbers)
                for key, kind in (('model', 'model'), ('by', 'source'), ('user', 'source'), ('id', 'reservation'), ('lease_id', 'lease')):
                    if key in value:
                        row[key] = self.label(kind, value[key])
                if 'status' in value:
                    row['status'] = value['status'] if value['status'] in ('pending', 'stale', 'confirmed', 'released') else 'stale'
                if 'reason' in value:
                    row['reason'] = 'redacted'
                result[name].append(row)
        result['memory'] = self.fields(source.get('memory', {}), ('host_available_gb', 'sleeping_weights_gb', 'budget_gb', 'host_min_available_gb'))
        result['errors'] = ['redacted collection error' for _ in source.get('errors', [])]
        return result

    def journal(self, lines):
        events, dropped = [], 0
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                message = record.get('MESSAGE', '')
                if not isinstance(message, str):
                    raise ValueError('unsupported journal message')
                try:
                    payload = json.loads(message)
                except ValueError:
                    payload = None
                timestamp = float(record['__REALTIME_TIMESTAMP']) / 1_000_000 if '__REALTIME_TIMESTAMP' in record else None
                if timestamp is not None and not math.isfinite(timestamp):
                    raise ValueError('invalid timestamp')
                if isinstance(payload, dict):
                    kind = payload.get('kind')
                    model = payload.get('model')
                    gpu = payload.get('gpu')
                else:
                    # Legacy text is distilled into known model/action labels;
                    # the raw line, command, path, user, and reason never leave.
                    models = [name for name in self.labels.get('model', {}) if re.search(r'(?<![\w.-])(?:vllm-)?' + re.escape(name) + r'(?:\.service)?(?![\w.-])', message)]
                    action = re.search(r'\b(sleep(?:ing)?|stop(?:ping|ped)?|wak(?:e|ing)|plac(?:e|ing)|start(?:ing|ed)?)\b', message, re.I)
                    match = re.search(r'\bGPU\s*[=:]?\s*(\d+)\b', message, re.I)
                    word = action.group(1).lower() if action else ''
                    kind = next((kind for stem, kind in (('sleep', 'sleep'), ('stop', 'stop'), ('wak', 'wake'), ('plac', 'place'), ('start', 'start')) if word.startswith(stem)), None)
                    model = models[0] if len(models) == 1 else None
                    gpu = int(match.group(1)) if match else None
                if kind not in KINDS or not isinstance(model, str) or not model:
                    raise ValueError('unsupported journal event')
                events.append({'timestamp': timestamp, 'kind': kind, 'model': self.label('model', model),
                               'gpu': gpu if type(gpu) is int and gpu >= 0 else None})
            except (ValueError, TypeError, AttributeError, OverflowError):
                dropped += 1
        return events, dropped


def export_snapshot(snapshot, journal_lines=()):
    sanitizer = Sanitizer()
    clean = sanitizer.snapshot(snapshot)
    journal, dropped = sanitizer.journal(journal_lines)
    return {'generated_by': 'Codex / gpt-6-astra', 'schema_version': 1,
            'provenance': 'sanitized input observations; expected actions not inferred',
            'snapshot': clean, 'journal_events': journal, 'journal_dropped': dropped,
            'expected_actions': None}


def read_limited(path):
    with Path(path).open('rb') as stream:
        value = stream.read(MAX_BYTES + 1)
    if len(value) > MAX_BYTES:
        raise ValueError('input exceeds export size limit')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--state', help='GET /v1/state JSON captured to a file')
    inputs.add_argument('--state-url', help='Full read-only /v1/state URL')
    parser.add_argument('--journal', help='Optional journalctl -o json JSONL capture')
    parser.add_argument('--output', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if args.state_url:
        url = urlsplit(args.state_url)
        if url.scheme not in ('http', 'https') or url.path != '/v1/state' or url.query or url.fragment or url.username or url.password:
            parser.error('state-url must be an HTTP(S) /v1/state endpoint')
        with build_opener(ProxyHandler({})).open(args.state_url, timeout=1) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('input exceeds export size limit')
    else:
        raw = read_limited(args.state)
    lines = read_limited(args.journal).decode().splitlines() if args.journal else ()
    result = export_snapshot(json.loads(raw), lines)
    if args.dry_run:
        print(json.dumps({'would_write': args.output, 'journal_dropped': result['journal_dropped']}))
        return 0
    output = Path(args.output)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=output.parent, prefix='.' + output.name,
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
