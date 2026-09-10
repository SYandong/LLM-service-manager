# Generated-By: Codex / gpt-6-astra
"""Bounded durable catalog checkpoint; no runtime/source authority by parsing."""

import json
import re

MAX_CHECKPOINT_BYTES = 1024 * 1024


def catalog_json(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode()) > MAX_CHECKPOINT_BYTES:
        raise ValueError("catalog checkpoint exceeds limit")
    return raw


def validate_checkpoint(record):
    required = {"transaction_id", "job_id", "old_epoch", "new_epoch", "phase", "base_sha256", "candidate_sha256",
                "marker_sha256", "marker_json", "binding", "old_manifest", "new_manifest"}
    if not isinstance(record, dict) or set(record) != required:
        raise ValueError("invalid catalog checkpoint")
    catalog_json(record)
    for key in ("transaction_id", "job_id", "old_epoch", "new_epoch"):
        if not isinstance(record[key], str) or not re.fullmatch(r"[0-9a-f]{32}", record[key]):
            raise ValueError("invalid catalog checkpoint identity")
    if record["old_epoch"] == record["new_epoch"]:
        raise ValueError("catalog epoch must advance")
    for key in ("base_sha256", "candidate_sha256"):
        if not isinstance(record[key], str) or not re.fullmatch(r"[0-9a-f]{64}", record[key]):
            raise ValueError("invalid catalog checkpoint digest")
    marker = record["marker_sha256"]
    if marker is not None and (not isinstance(marker, str) or not re.fullmatch(r"[0-9a-f]{64}", marker)):
        raise ValueError("invalid catalog marker digest")
    raw = record["marker_json"]
    if marker is not None:
        import hashlib
        if not isinstance(raw, str) or len(raw.encode()) > 65536 or hashlib.sha256(raw.encode()).hexdigest() != marker:
            raise ValueError("catalog marker receipt mismatch")
    elif raw is not None:
        raise ValueError("catalog marker digest absent")
    if record["phase"] not in ("claimed", "published", "released", "aborted"):
        raise ValueError("invalid catalog phase")
    if record["phase"] in ("published", "released") and marker is None:
        raise ValueError("catalog publication lacks marker binding")
    from llmsvc.reload_witness import CandidateBinding
    binding = CandidateBinding.from_dict(record["binding"])
    if binding.candidate_sha256 != record["candidate_sha256"]:
        raise ValueError("catalog candidate binding mismatch")
    for key in ("old_manifest", "new_manifest"):
        value = record[key]
        if (not isinstance(value, dict) or set(value) != {"sources", "active", "retained"}
                or not all(isinstance(value[item], dict) for item in value)
                or set(value["active"]) & set(value["retained"])):
            raise ValueError("invalid catalog manifest")

    if record["old_manifest"]["sources"] != record["new_manifest"]["sources"]:
        raise ValueError("catalog cannot change global sources")
    if raw is not None:
        receipt = json.loads(raw)
        if (not isinstance(receipt, dict) or receipt.get("sha256") != record["candidate_sha256"]
                or receipt.get("witness_binding") != record["binding"]
                or not isinstance(receipt.get("job"), dict) or receipt["job"].get("id") != record["job_id"]):
            raise ValueError("catalog marker transaction mismatch")
