# Generated-By: Codex / gpt-6-astra
"""Read-only pinned-v252 active-generation evidence; never a reload notifier.

Request/envelope semantics come from deploy/watcher_witness.py and #91. The
transport accepts direct HTTP IP endpoints only: no DNS, proxies or redirects.
A socket watchdog bounds headers/body, including a peer that trickles bytes.
No file/process inspection is implicit; binding observations belong to the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import ipaddress
import json
import math
import re
import socket
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
import uuid

import yaml

PINNED_COMMIT = "e31a1adee494bb7a578e2a97ec891b3e809899dc"
PROTOCOL_VERSION = "2026-07-28"
GENERATION_PATH = "macros.llmsvc_reload_generation"
TOOL_NAME = "config__get_config"
_GENERATION = re.compile(r"gen_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PREFIX = ('Current llama-swap configuration at "' + GENERATION_PATH
           + '" (credentials redacted, values resolved):\n\n```yaml\n')
_SUFFIX = "\n```\n"


class WitnessError(ValueError):
    """Stable, non-sensitive failure code; response bodies are never echoed."""


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_generation(envelope: object, request_id: str) -> str:
    """Extract only the complete native scalar, matching the proven #91 parser."""
    if (not isinstance(request_id, str) or not request_id or not isinstance(envelope, dict)
            or envelope.get("jsonrpc") != "2.0" or envelope.get("id") != request_id
            or "error" in envelope):
        raise WitnessError("invalid_rpc_response")
    result = envelope.get("result")
    if not isinstance(result, dict) or result.get("isError", False) is not False:
        raise WitnessError("native_tool_error")
    content = result.get("content")
    if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
            or content[0].get("type") != "text"):
        raise WitnessError("invalid_content_shape")
    text = content[0].get("text")
    if not isinstance(text, str):
        raise WitnessError("invalid_yaml_envelope")
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeError as exc:
        raise WitnessError("invalid_yaml_envelope") from exc
    if encoded_size > 32768 or not text.startswith(_PREFIX) or not text.endswith(_SUFFIX):
        raise WitnessError("invalid_yaml_envelope")
    try:
        generation = yaml.safe_load(text[len(_PREFIX):-len(_SUFFIX)])
    except (yaml.YAMLError, RecursionError) as exc:
        raise WitnessError("invalid_generation_scalar") from exc
    if not isinstance(generation, str) or not _GENERATION.fullmatch(generation):
        raise WitnessError("invalid_generation_scalar")
    return generation


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise WitnessError("invalid_json")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise WitnessError("invalid_json")


@dataclass(frozen=True)
class GenerationRead:
    endpoint: str
    request_id: str
    started_at: float
    received_at: float
    deadline: float
    generation: str | None = None
    http_status: int | None = None
    error: str | None = None


class NativeGenerationReader:
    """One bounded read-only POST, with no retry, signal or file mutation.

    `deadline` uses time.monotonic; request_timeout is a per-read total budget,
    defaulting to the existing fixture's 0.5 seconds. Callers may choose a longer
    read budget explicitly; it never extends the supplied transaction deadline.
    """

    def __init__(self, base_url: str, *, request_timeout: float = 0.5,
                 max_response_bytes: int = 65536, clock: Callable[[], float] = time.monotonic):
        if not isinstance(base_url, str) or any(char.isspace() for char in base_url):
            raise ValueError("base_url must be a direct HTTP IP URL")
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or not parsed.hostname or "%" in parsed.hostname):
            raise ValueError("base_url must be a direct HTTP IP URL")
        try:
            address = ipaddress.ip_address(parsed.hostname)
            port = parsed.port or 80
        except ValueError as exc:
            raise ValueError("DNS names and invalid ports are unsupported") from exc
        if parsed.port == 0:
            raise ValueError("port must be positive")
        if not _finite(request_timeout) or not 0 < request_timeout <= 60:
            raise ValueError("request_timeout must be positive and at most 60 seconds")
        if (isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int)
                or not 0 < max_response_bytes <= 65536):
            raise ValueError("max_response_bytes must be between 1 and 65536")
        host = str(address)
        authority = f"[{host}]:{port}" if address.version == 6 else f"{host}:{port}"
        self.endpoint = f"http://{authority}/api/mcp"
        self._host_header, self._host, self._port = authority, host, port
        self._family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        self.request_timeout, self.max_response_bytes, self.clock = request_timeout, max_response_bytes, clock

    def read(self, *, deadline: float) -> GenerationRead:
        if not _finite(deadline):
            raise ValueError("deadline must be finite monotonic time")
        started = self.clock()
        end = min(deadline, started + self.request_timeout)
        request_id = uuid.uuid4().hex
        status, generation, error = None, None, None
        expired = threading.Event()

        def remaining() -> float:
            left = end - self.clock()
            if expired.is_set() or left <= 0:
                raise WitnessError("deadline_expired")
            return left

        try:
            remaining()  # An expired deadline opens no socket.
            body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                               "params": {"name": TOOL_NAME, "arguments": {"path": GENERATION_PATH}}}).encode()
            headers = ("POST /api/mcp HTTP/1.1\r\n"
                       f"Host: {self._host_header}\r\nContent-Type: application/json\r\n"
                       f"Mcp-Protocol-Version: {PROTOCOL_VERSION}\r\n"
                       f"Mcp-Method: tools/call\r\nMcp-Name: {TOOL_NAME}\r\n"
                       f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("ascii")
            with socket.socket(self._family, socket.SOCK_STREAM) as sock:
                def interrupt_read() -> None:
                    expired.set()
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                timer = threading.Timer(remaining(), interrupt_read)
                timer.daemon = True
                timer.start()
                try:
                    sock.settimeout(remaining())
                    sock.connect((self._host, self._port))
                    sock.settimeout(remaining())
                    sock.sendall(headers + body)
                    sock.settimeout(remaining())
                    with http.client.HTTPResponse(sock, method="POST") as response:
                        response.begin()
                        remaining()
                        status = response.status
                        if 300 <= status < 400:
                            raise WitnessError("redirect_rejected")
                        if status != 200:
                            raise WitnessError("http_error")
                        if response.headers.get_content_type() != "application/json":
                            raise WitnessError("invalid_content_type")
                        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                            raise WitnessError("unsupported_content_encoding")
                        lengths = response.headers.get_all("Content-Length", [])
                        transfer = response.headers.get_all("Transfer-Encoding", [])
                        if (len(lengths) > 1 or (lengths and transfer)
                                or (lengths and not re.fullmatch(r"[0-9]+", lengths[0]))
                                or (transfer and transfer != ["chunked"])):
                            raise WitnessError("invalid_http_framing")
                        if lengths and int(lengths[0]) > self.max_response_bytes:
                            raise WitnessError("response_too_large")
                        sock.settimeout(remaining())
                        payload = response.read(self.max_response_bytes + 1)
                        remaining()
                        if len(payload) > self.max_response_bytes:
                            raise WitnessError("response_too_large")
                        if response.length not in (None, 0):
                            raise WitnessError("truncated_response")
                    envelope = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object,
                                          parse_constant=_invalid_constant)
                    generation = parse_generation(envelope, request_id)
                    remaining()
                finally:
                    timer.cancel()
                    timer.join()
        except WitnessError as exc:
            error = str(exc)
        except (TimeoutError, socket.timeout):
            error = "deadline_expired"
        except (OSError, http.client.HTTPException):
            error = "transport_error"
        except (ValueError, UnicodeError, RecursionError):
            error = "invalid_json"
        received = self.clock()
        if expired.is_set() or received >= end:
            error = "deadline_expired"
        if error:
            generation = None
        return GenerationRead(self.endpoint, request_id, started, received, end, generation, status, error)


@dataclass(frozen=True)
class InstanceIdentity:
    pid: int
    start_ticks: str


@dataclass(frozen=True)
class CandidateBinding:
    endpoint: str
    generation: str
    instance: InstanceIdentity
    candidate_sha256: str


@dataclass(frozen=True)
class BindingObservation:
    observed_at: float
    instance: InstanceIdentity | None
    candidate_sha256: str | None


@dataclass(frozen=True)
class VisibilityCheck:
    candidate_generation_visible: bool
    reasons: tuple[str, ...]
    # This source supplies no independent successful-teardown ACK.
    settlement_confirmed: None = field(default=None, init=False)


def _valid_instance(instance: InstanceIdentity | None) -> bool:
    return (isinstance(instance, InstanceIdentity) and type(instance.pid) is int and instance.pid > 0
            and isinstance(instance.start_ticks, str) and bool(re.fullmatch(r"[0-9]+", instance.start_ticks)))


def check_visibility(expected: CandidateBinding, before: BindingObservation,
                     reading: GenerationRead, after: BindingObservation, *,
                     now: float, max_age: float = 5) -> VisibilityCheck:
    """Pure check of supplied observations, not an instance/file/settlement probe.

    Observations must bracket the read, be fresh and match the expected instance
    and bytes. Equality only reports those sampled observations; it cannot rule
    out unobserved restarts, changed-and-restored files or external writers.
    """
    if (not _finite(now) or not _finite(max_age) or max_age <= 0
            or not isinstance(expected.generation, str) or not _GENERATION.fullmatch(expected.generation)
            or not isinstance(expected.candidate_sha256, str) or not _DIGEST.fullmatch(expected.candidate_sha256)
            or not _valid_instance(expected.instance)):
        raise ValueError("invalid expected binding or freshness bound")
    reasons = []
    if reading.endpoint != expected.endpoint:
        reasons.append("endpoint_mismatch")
    if reading.error or reading.http_status != 200 or reading.generation != expected.generation:
        reasons.append("native_generation_not_confirmed")
    times = (before.observed_at, reading.started_at, reading.received_at, after.observed_at, now)
    if (not all(_finite(value) for value in times) or tuple(sorted(times)) != times
            or now - before.observed_at > max_age):
        reasons.append("observations_stale_or_unordered")
    if not _finite(reading.deadline) or not _finite(now) or now >= reading.deadline:
        reasons.append("deadline_expired")
    for observation in (before, after):
        if not _valid_instance(observation.instance):
            reasons.append("instance_unknown")
        elif observation.instance != expected.instance:
            reasons.append("service_identity_changed")
        if observation.candidate_sha256 != expected.candidate_sha256:
            reasons.append("candidate_file_digest_unconfirmed")
    return VisibilityCheck(not reasons, tuple(dict.fromkeys(reasons)))
