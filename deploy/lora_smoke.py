#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Ops-only synthetic LoRA mechanics using the existing bounded lifecycle runner."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import time

import lifecycle_smoke as lifecycle


class LoRARun(lifecycle.Run):
    scope = 'synthetic zero-delta LoRA mechanics; not trained-model quality or reload'
    lora_measured = True

    def extra_env(self):
        return {'VLLM_ALLOW_RUNTIME_LORA_UPDATING': 'True'}

    def extra_args(self):
        # vLLM max-lora-rank accepts8 as its minimum capacity; the fixture rank is1.
        return ['--enable-lora', '--max-loras', '1', '--max-cpu-loras', '1',
                '--max-lora-rank', '8']

    def prepare_fixture(self):
        self.adapter_name = self.model + '-synthetic-zero'
        self.adapter_path = self.temp + '/adapter'
        source = Path(__file__).with_name('synthetic_lora.py').read_text()
        code = '''import json,sys
from pathlib import Path
x=json.load(sys.stdin)
if (Path(x['root'])/'owner').read_text()!=x['token']:raise RuntimeError('fixture ownership mismatch')
ns={'__name__':'synthetic_lora'};exec(compile(x['source'],'<synthetic_lora>','exec'),ns)
print(json.dumps(ns['generate'](x['base'],x['output'])))
'''
        result = self.python(code, {'root': self.temp, 'token': self.token,
                                   'source': source, 'base': self.config['model_path'],
                                   'output': self.adapter_path}, limit=10)
        self.log('synthetic_fixture', generator_sha256=hashlib.sha256(source.encode()).hexdigest(),
                 adapter=self.adapter_name, **result)

    def budget(self):
        # Stop adding phases while at least60s of total wall budget remains.
        if self.deadline - time.monotonic() <= 60:
            raise lifecycle.SmokeError('insufficient phase budget; retain partial evidence and clean up')

    def measured(self, phase, path, payload=None, *, limit=15):
        self.budget()
        self.inventory(allow_own=True)
        self.check_endpoint()
        started = time.monotonic()
        result = self.http(path, payload, limit=limit)
        self.log('http_measurement', phase=phase, seconds=time.monotonic()-started,
                 status=result['status'])
        if result['status'] != 200:
            raise lifecycle.SmokeError('phase HTTP failure: ' + phase)
        return result

    def adapter_listed(self, expected):
        ids = [row.get('id') for row in json.loads(self.http('/v1/models')['body']).get('data', [])]
        if (self.adapter_name in ids) != expected:
            raise lifecycle.SmokeError('adapter list mismatch')
        self.log('adapter_list', listed=expected)

    def inference(self, phase, model):
        result = self.measured(phase, '/v1/chat/completions',
                               {'model': model, 'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
                                'max_tokens': 8, 'temperature': 0, 'seed': 7})
        answer = json.loads(result['body'])
        choices = answer.get('choices') or []
        if not choices or not isinstance(choices[0].get('message', {}).get('content'), str):
            raise lifecycle.SmokeError('inference response has no message')
        text = choices[0]['message']['content']
        if not text:
            raise lifecycle.SmokeError('inference returned empty content')
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.log('inference', phase=phase, requested_model=model, content_sha256=digest,
                 completion_tokens=answer.get('usage', {}).get('completion_tokens'))
        return digest

    def memory(self, phase):
        gpu = self.inventory(allow_own=True)
        raw = self.container(['systemctl', 'show', self.unit, '-p', 'MemoryCurrent', '--value']).stdout.strip()
        # Cgroup memory is this test unit's accounting, not trusted host MemAvailable.
        unit_bytes = int(raw) if raw.isdecimal() else None
        self.log('memory', phase=phase, gpu_used_mib=float(gpu[3]), unit_memory_current_bytes=unit_bytes,
                 comparison='same LoRA-enabled daemon before/after runtime load; static enable overhead unmeasured')

    def exercise(self):
        self.collect('awake')
        self.memory('before_load')
        baseline = self.inference('base_before_load', self.model)
        self.measured('load_adapter', '/v1/load_lora_adapter',
                      {'lora_name': self.adapter_name, 'lora_path': self.adapter_path})
        self.adapter_listed(True)
        before = self.inference('adapter_before_sleep', self.adapter_name)
        self.memory('after_load_and_request')
        self.measured('sleep_level1', '/sleep?level=1&mode=wait', {})
        self.collect('sleeping')
        self.memory('sleeping')
        self.measured('wake_level1', '/wake_up', {})
        self.collect('awake')
        self.adapter_listed(True)  # No second load; a list alone is not retention proof.
        after = self.inference('adapter_after_wake', self.adapter_name)
        self.memory('after_wake_and_request')
        self.measured('unload_adapter', '/v1/unload_lora_adapter', {'lora_name': self.adapter_name})
        self.adapter_listed(False)
        self.log('synthetic_retention', adapter_request_after_wake=True,
                 base_and_adapter_text_equal=baseline == before == after,
                 trained_quality_measured=False, nonzero_delta_semantics_measured=False,
                 static_lora_enable_overhead_measured=False, swap_routing_measured=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    lifecycle.validate(config)
    if args.dry_run:
        print(json.dumps({'dry_run': True, 'scope': LoRARun.scope, 'wall_seconds': config.get('wall_seconds', 300),
                          'fixture': 'rank1 all-zero Qwen2 q_proj, generated only inside owned temporary root',
                          'creates_files_or_locks_or_processes': False}))
        return 0
    output = Path(config['output_dir'])
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with Path(config['lock_path']).open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run = LoRARun(config)
        result = run.execute()
        path = output / (run.model + '-lora.json')
        with path.open('x') as stream:
            path.chmod(0o600)
            json.dump({'unit': run.unit, 'token': run.token, 'records': run.records}, stream, indent=2)
        print(json.dumps({'evidence': str(path)}))
    return 0 if result == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
