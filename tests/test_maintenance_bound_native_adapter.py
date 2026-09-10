# Generated-By: Codex / gpt-6-astra
"""Actual NativeAdapter observation dispatch with the bound core reader mounted.

Unit/helper facts remain the existing CPU fixture; no projected adapter result
or real native/site execution is used to claim compatibility.
"""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from llmsvc.maintenance import MaintenanceError
from llmsvc.native_binding import build_bound_generation_reader
from llmsvc.reload_witness import PINNED_COMMIT
from test_catalog_lifecycle import catalog
from test_maintenance_native_integration import native_integration
from test_maintenance_lifecycle import enqueue


def test_actual_native_adapter_supplies_independent_file_proof_for_bound_reader(native_integration):
    c = native_integration
    image = hashlib.sha256(Path('/proc/'+str(c.backend.old.pid)+'/exe').read_bytes()).hexdigest()
    settings = {'images':[{'source_commit':PINNED_COMMIT,'executable_sha256':image,'dialect':'v252-path'}],
                'phase_images':dict.fromkeys(['old','candidate','restored'],image),
                'request_timeout_seconds':2}
    c.s.config = replace(c.s.config,native_witness=settings)
    c.controller.native_reader = build_bound_generation_reader(c.s.config,
        instance_provider=c.controller.inspect_instance,config_reader=c.q._read,
        config_provider=lambda:c.s.config,clock=c.q.clock)
    # Exact current fixture image pin; the existing fixture supplies synthetic
    # service scope only. Do not replace operation() or its returned proof fields.
    c.native_adapter.profile['native_binary_sha256'] = image
    actual_request = c.controller._request
    failures = []
    def traced_request(*args, **kwargs):
        try:
            return actual_request(*args, **kwargs)
        except MaintenanceError as exc:
            failures.append(str(exc))
            raise
    c.controller._request = traced_request
    enqueue(c)
    result = c.runtime.process_once()
    assert result['status'] == 'applied', json.dumps({'result':result,'controller_errors':failures}, sort_keys=True)
    record = c.store.maintenance_checkpoint(c.store.catalog_checkpoint()['transaction_id'])
    assert record['stage'] == 'released' and not c.s.catalog_fenced
    assert record['observations']['candidate']['native_visibility']['pin']['executable_sha256'] == image
