#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Generate a tiny, explicitly synthetic Qwen2 zero-delta LoRA; no base writes."""
import argparse
import hashlib
import json
from pathlib import Path
import struct


LABEL = 'synthetic-zero-delta-mechanics-only'


def fixture(config_bytes, base_name):
    config = json.loads(config_bytes)
    if config.get('model_type') != 'qwen2' or config.get('architectures') != ['Qwen2ForCausalLM']:
        raise ValueError('only the inspected Qwen2ForCausalLM fixture shape is supported')
    hidden, layers, heads = (config.get(k) for k in ('hidden_size', 'num_hidden_layers', 'num_attention_heads'))
    if any(type(x) is not int or x <= 0 for x in (hidden, layers, heads)):
        raise ValueError('positive integer Qwen2 dimensions required')
    if hidden % heads or config.get('head_dim', hidden // heads) != hidden // heads:
        raise ValueError('unsupported q_proj dimensions')
    if hidden > 8192 or layers > 80:
        raise ValueError('fixture dimensions exceed bounded small-model limit')
    metadata = {'format': 'pt', 'fixture': LABEL, 'Generated-By': 'Codex / gpt-6-astra'}
    header = {'__metadata__': metadata}
    offset = 0
    for layer in range(layers):
        for side, shape in (('A', [1, hidden]), ('B', [hidden, 1])):
            name = f'base_model.model.model.layers.{layer}.self_attn.q_proj.lora_{side}.weight'
            size = hidden * 2  # rank-one BF16; both factors are exactly zero.
            header[name] = {'dtype': 'BF16', 'shape': shape, 'data_offsets': [offset, offset + size]}
            offset += size
    encoded = json.dumps(header, separators=(',', ':'), sort_keys=True).encode()
    encoded += b' ' * (-len(encoded) % 8)
    weights = struct.pack('<Q', len(encoded)) + encoded + bytes(offset)
    adapter = {'base_model_name_or_path': base_name, 'peft_type': 'LORA',
               'task_type': 'CAUSAL_LM', 'r': 1, 'lora_alpha': 1, 'lora_dropout': 0.0,
               'target_modules': ['q_proj'], 'bias': 'none', 'inference_mode': True}
    config_out = (json.dumps(adapter, indent=2, sort_keys=True) + '\n').encode()
    files = {'adapter_config.json': config_out, 'adapter_model.safetensors': weights}
    manifest = {'fixture': LABEL, 'trained': False, 'quality_evidence': False,
                'base_config_sha256': hashlib.sha256(config_bytes).hexdigest(),
                'rank': 1, 'layers': layers, 'hidden_size': hidden,
                'target_modules': ['q_proj'], 'tensor_count': 2 * layers,
                'all_factors_zero': True,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                'generated_by': 'Codex / gpt-6-astra'}
    files['fixture.json'] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    return files, manifest


def generate(base, output, *, dry_run=False):
    base, output = Path(base).resolve(strict=True), Path(output)
    parent = output.parent.resolve(strict=True)
    destination = parent / output.name
    if destination == base or base in destination.parents:
        raise ValueError('fixture output must be outside the cached base')
    if destination.exists() or destination.is_symlink():
        raise ValueError('fixture destination must not exist')
    config_bytes = (base / 'config.json').read_bytes()
    files, manifest = fixture(config_bytes, str(base))
    if not dry_run:
        destination.mkdir(mode=0o700)
        for name, data in files.items():
            with (destination / name).open('xb') as stream:
                stream.write(data)
            (destination / name).chmod(0o600)
    return {**manifest, 'dry_run': dry_run, 'bytes': sum(map(len, files.values()))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(generate(args.base, args.output, dry_run=args.dry_run)))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
