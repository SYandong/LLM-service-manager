# Generated-By: Codex / gpt-6-astra
"""Schema-six transition records; parsing grants no execution authority."""

import hashlib
import json

MAX_MAINTENANCE_BYTES = 4 * 1024 * 1024
EFFECTS = {"exclude", "stop_old", "start_candidate", "resume", "stop_candidate", "start_base", "restore_config"}


def validate_maintenance(record):
    from llmsvc.maintenance import identity
    expected = {"transaction_id", "job_id", "mode", "old_identity", "new_identity", "rollback_identity",
                "base_bytes", "candidate_sha256", "base_sha256", "generation", "stage", "effects",
                "observations", "error", "accounts", "rollback_epoch", "backend_bindings", "exclusion_method", "backend_sha256", "operation_id", "observed_scope", "observed_actors", "new_scope", "new_actors", "rollback_scope", "rollback_actors"}
    if not isinstance(record, dict) or set(record) != expected or record["mode"] != "maintenance":
        raise ValueError("invalid maintenance checkpoint")
    raw = json.dumps(record, allow_nan=False)
    if len(raw.encode()) > MAX_MAINTENANCE_BYTES:
        raise ValueError("maintenance checkpoint exceeds limit")
    for key in ("transaction_id", "job_id", "rollback_epoch", "operation_id"):
        if not isinstance(record[key], str) or len(record[key]) != 32 or any(c not in "0123456789abcdef" for c in record[key]):
            raise ValueError("invalid maintenance transaction")
    identity(record["old_identity"])
    for key in ("new_identity", "rollback_identity"):
        if record[key] is not None:
            identity(record[key])
            if record[key] == record["old_identity"]:
                raise ValueError("maintenance needs a distinct new instance")
    for key in ("base_sha256", "candidate_sha256", "backend_sha256"):
        if not isinstance(record[key], str) or len(record[key]) != 64 or any(c not in "0123456789abcdef" for c in record[key]):
            raise ValueError("invalid maintenance configuration hash")
    if not isinstance(record["base_bytes"], str) or hashlib.sha256(record["base_bytes"].encode()).hexdigest() != record["base_sha256"]:
        raise ValueError("maintenance backup hash mismatch")
    if (not isinstance(record["generation"], str) or not record["generation"].startswith("gen_")
            or len(record["generation"]) != 36 or any(c not in "0123456789abcdef" for c in record["generation"][4:])):
        raise ValueError("invalid maintenance generation")
    if record["exclusion_method"] != "stop_instance":
        raise ValueError("unknown maintenance exclusion method")
    if (not isinstance(record["observed_scope"], dict) or not isinstance(record["observed_actors"], list)
            or not 1 <= len(record["observed_actors"]) <= 4096):
        raise ValueError("unknown maintenance process scope")
    if hashlib.sha256(json.dumps(record["observed_scope"],sort_keys=True,separators=(",", ":"),allow_nan=False).encode()).hexdigest() != record["old_identity"]["scope_sha256"]:
        raise ValueError("maintenance process scope hash mismatch")
    for actor in record["observed_actors"]:
        identity(actor)
    if record["old_identity"] not in record["observed_actors"]:
        raise ValueError("maintenance source missing from actor inventory")
    if record["stage"] not in EFFECTS | {"claimed", "old_settled", "adopted", "released", "rollback_settled", "rolled_back", "stop_model", "aborted", "base_verified"}:
        raise ValueError("invalid maintenance stage")
    if not isinstance(record["effects"], dict) or not isinstance(record["observations"], dict) or not isinstance(record["accounts"], list) or not isinstance(record["backend_bindings"], list):
        raise ValueError("invalid maintenance progress")
    for operation, value in record["effects"].items():
        if operation not in EFFECTS and not operation.startswith("stop_model:"):
            raise ValueError("unsupported maintenance effect")
        if (not isinstance(value, dict) or set(value) != {"submitted", "acknowledged"}
                or value["submitted"] is not True or type(value["acknowledged"]) is not bool):
            raise ValueError("invalid maintenance effect receipt")

    for prefix in ("new", "rollback"):
        scope, actors = record[prefix+"_scope"], record[prefix+"_actors"]
        if not isinstance(actors, list) or len(actors) > 4096:
            raise ValueError("invalid transition actor inventory")
        if scope is not None:
            expected_identity = record[prefix+"_identity"]
            if (expected_identity is None or not isinstance(scope, dict)
                    or hashlib.sha256(json.dumps(scope,sort_keys=True,separators=(",", ":"),allow_nan=False).encode()).hexdigest() != expected_identity["scope_sha256"]
                    or expected_identity not in actors):
                raise ValueError("invalid transition scope binding")
            for actor in actors:
                identity(actor)
