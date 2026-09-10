# Generated-By: Codex / gpt-6-astra
"""Local running-image and socket binding around the existing native reader.

This supplies sampled visibility only, never helper/resource settlement. The
instance provider is the configured service inspector, not an HTTP parameter.
"""

import copy
import hashlib
import ipaddress
import math
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from llmsvc.reload_witness import (
    BindingObservation, CandidateBinding, GenerationRead, InstanceIdentity,
    NativeGenerationReader, PINNED_COMMIT, QUERY_DIALECT, QUERY_PINNED_COMMIT,
    V252_DIALECT, VisibilityCheck, WitnessError, check_visibility,
)


@dataclass(frozen=True)
class ImagePin:
    source_commit: str
    executable_sha256: str
    dialect: str


@dataclass(frozen=True)
class BoundGenerationRead:
    reading: GenerationRead
    visibility: VisibilityCheck
    pin: ImagePin
    instance: InstanceIdentity


@dataclass(frozen=True)
class _Observation:
    binding: BindingObservation
    pin: ImagePin
    image_stat: tuple
    config_stat: tuple
    listener_inode: str


def validate_settings(value):
    if not isinstance(value, dict):
        raise ValueError('native_witness must be a mapping')
    if not value:
        return ()
    if set(value) - {'images', 'request_timeout_seconds', 'max_image_bytes'} or 'images' not in value:
        raise ValueError('invalid native_witness keys')
    images = value['images']
    if not isinstance(images, list) or not 1 <= len(images) <= 16:
        raise ValueError('native_witness images must contain 1..16 pins')
    pins = []
    sources = {V252_DIALECT: PINNED_COMMIT, QUERY_DIALECT: QUERY_PINNED_COMMIT}
    for item in images:
        if (not isinstance(item, dict) or set(item) != {'source_commit', 'executable_sha256', 'dialect'}
                or not isinstance(item['dialect'], str) or sources.get(item['dialect']) != item['source_commit']
                or not isinstance(item['executable_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', item['executable_sha256'])):
            raise ValueError('native_witness needs an explicit supported source/artifact/dialect pin')
        pin = ImagePin(**item)
        if any(old.executable_sha256 == pin.executable_sha256 for old in pins):
            raise ValueError('duplicate native_witness image digest')
        pins.append(pin)
    timeout = value.get('request_timeout_seconds', 0.5)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError('native_witness timeout must be in (0,60]')
    size = value.get('max_image_bytes', 128 * 1024 * 1024)
    if type(size) is not int or not 1 <= size <= 512 * 1024 * 1024:
        raise ValueError('native_witness image limit must be in 1..536870912')
    return tuple(pins)


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class BoundNativeGenerationReader:
    def __init__(self, base_url, settings, *, instance_provider, config_reader,
                 settings_provider=None, clock=time.monotonic):
        self._settings = copy.deepcopy(settings)
        self._pins = validate_settings(self._settings)
        if not self._pins or not callable(instance_provider) or not callable(config_reader):
            raise ValueError('native witness needs pins and configured instance/configuration readers')
        self.instance_provider, self.config_reader = instance_provider, config_reader
        self.settings_provider, self.clock = settings_provider, clock
        self._readers = {pin.executable_sha256: NativeGenerationReader(
            base_url, dialect=pin.dialect, clock=clock,
            request_timeout=settings.get('request_timeout_seconds', 0.5)) for pin in self._pins}
        self.endpoint = next(iter(self._readers.values())).endpoint
        parts = urlsplit(self.endpoint)
        self.address, self.port = ipaddress.ip_address(parts.hostname), parts.port
        self._max_image = settings.get('max_image_bytes', 128 * 1024 * 1024)

    def _remaining(self, deadline):
        if type(deadline) not in (int, float) or not math.isfinite(deadline) or self.clock() >= deadline:
            raise WitnessError('binding_deadline_expired')
        if self.settings_provider is not None and self.settings_provider() != self._settings:
            raise WitnessError('witness_configuration_changed')

    def _small(self, path, limit, deadline):
        self._remaining(deadline)
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise WitnessError('binding_source_not_regular')
            parts, count = [], 0
            while True:
                self._remaining(deadline)
                block = os.read(fd, min(65536, limit + 1 - count))
                if not block:
                    return b''.join(parts)
                parts.append(block); count += len(block)
                if count > limit:
                    raise WitnessError('binding_source_too_large')
        finally:
            os.close(fd)

    def _process(self, expected, deadline):
        if (not isinstance(expected, InstanceIdentity) or type(expected.pid) is not int or expected.pid <= 0
                or not isinstance(expected.start_ticks, str) or not re.fullmatch('[0-9]{1,32}', expected.start_ticks)
                or str(int(expected.start_ticks)) != expected.start_ticks):
            raise WitnessError('invalid_expected_instance')
        raw = self._small(Path('/proc') / str(expected.pid) / 'stat', 16384, deadline)
        fields = raw[raw.rfind(b')')+2:].split()
        if len(fields) < 20 or fields[0] in (b'Z', b'X', b'x') or fields[19].decode('ascii') != expected.start_ticks:
            raise WitnessError('running_instance_changed')
        for namespace in ('pid', 'net'):
            own = os.stat('/proc/self/ns/' + namespace)
            target = os.stat('/proc/' + str(expected.pid) + '/ns/' + namespace)
            if (own.st_dev, own.st_ino) != (target.st_dev, target.st_ino):
                raise WitnessError('process_namespace_unbound')

    def _image(self, expected, deadline):
        path = '/proc/' + str(expected.pid) + '/exe'
        self._process(expected, deadline)
        # Deliberately follow this kernel process magic link, not a configured
        # executable pathname that can point at a different deployment image.
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= self._max_image:
                raise WitnessError('running_image_unavailable_or_too_large')
            digest, size = hashlib.sha256(), 0
            while True:
                self._remaining(deadline)
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > self._max_image:
                    raise WitnessError('running_image_too_large')
                digest.update(chunk)
            signature = _file_identity(before)
            if size != before.st_size or signature != _file_identity(os.fstat(fd)) or signature != _file_identity(os.stat(path)):
                raise WitnessError('running_image_changed')
        finally:
            os.close(fd)
        self._process(expected, deadline)
        pin = next((item for item in self._pins if item.executable_sha256 == digest.hexdigest()), None)
        if pin is None:
            raise WitnessError('running_image_not_pinned')
        return pin, signature

    @staticmethod
    def _address(value, ipv6):
        raw = bytes.fromhex(value)
        width = 16 if ipv6 else 4
        if len(raw) != width:
            raise WitnessError('listener_table_invalid')
        if sys.byteorder == 'little':
            raw = b''.join(raw[i:i+4][::-1] for i in range(0, width, 4))
        return ipaddress.ip_address(raw)

    def _listener(self, expected, deadline):
        matches = set()
        for table, ipv6 in (('tcp', False), ('tcp6', True)):
            try:
                raw = self._small(Path('/proc') / str(expected.pid) / 'net' / table, 4 * 1024 * 1024, deadline)
            except FileNotFoundError:
                if ipv6 and self.address.version == 4:
                    continue  # Kernel may have IPv6 disabled; other errors block.
                raise
            for line in raw.decode('ascii').splitlines()[1:]:
                fields = line.split()
                if len(fields) < 10:
                    raise WitnessError('listener_table_invalid')
                if fields[3] != '0A':
                    continue
                address, port = fields[1].split(':')
                if int(port, 16) != self.port:
                    continue
                address = self._address(address, ipv6)
                normalized = getattr(address, 'ipv4_mapped', None) or address
                if normalized == self.address or address.is_unspecified:
                    matches.add(fields[9])
        if len(matches) != 1 or '0' in matches:
            raise WitnessError('endpoint_listener_ambiguous')
        listener = next(iter(matches))
        owned = False
        with os.scandir('/proc/' + str(expected.pid) + '/fd') as entries:
            for count, entry in enumerate(entries):
                self._remaining(deadline)
                if count >= 4096:
                    raise WitnessError('process_descriptor_limit')
                try:
                    target = os.readlink(entry.path)
                except FileNotFoundError:
                    continue  # Unrelated short-lived descriptors may close.
                if target == 'socket:[' + listener + ']':
                    owned = True
        if not owned:
            raise WitnessError('endpoint_not_owned_by_instance')
        self._process(expected, deadline)
        return listener

    def _observe(self, expected, deadline):
        self._remaining(deadline)
        if self.instance_provider(deadline=deadline) != expected:
            raise WitnessError('configured_service_instance_changed')
        pin, image = self._image(expected, deadline)
        listener = self._listener(expected, deadline)
        raw, info = self.config_reader()
        self._remaining(deadline)
        if (not isinstance(raw, bytes) or self.instance_provider(deadline=deadline) != expected
                or image != _file_identity(os.stat('/proc/' + str(expected.pid) + '/exe'))):
            raise WitnessError('binding_changed_during_observation')
        self._process(expected, deadline)
        return _Observation(BindingObservation(self.clock(), expected, hashlib.sha256(raw).hexdigest()),
                            pin, image, _file_identity(info), listener)

    def read(self, expected, *, deadline):
        """Perform exactly one selected native read between local observations."""
        if not isinstance(expected, CandidateBinding) or expected.endpoint != self.endpoint:
            raise WitnessError('configured_endpoint_mismatch')
        expected.to_dict()  # Existing strict public binding validation.
        try:
            before = self._observe(expected.instance, deadline)
            if before.binding.candidate_sha256 != expected.candidate_sha256:
                raise WitnessError('candidate_file_digest_unconfirmed')
            reader = self._readers[before.pin.executable_sha256]
            reading = reader.read(deadline=deadline)
            after = self._observe(expected.instance, min(deadline, reading.deadline))
            if (before.pin != after.pin or before.image_stat != after.image_stat
                    or before.config_stat != after.config_stat or before.listener_inode != after.listener_inode):
                raise WitnessError('native_binding_changed')
            now = self.clock()
            self._remaining(min(deadline, reading.deadline))
            visibility = check_visibility(expected, before.binding, reading, after.binding,
                                          now=now)
            return BoundGenerationRead(reading, visibility, before.pin, expected.instance)
        except WitnessError:
            raise
        except (OSError, ValueError, UnicodeError, IndexError, TypeError, AttributeError) as exc:
            raise WitnessError('native_binding_unavailable') from exc


def build_bound_generation_reader(config, *, instance_provider, config_reader,
                                  config_provider=None, clock=time.monotonic):
    if not config.native_witness:
        return None
    base_url = config.collectors.get('swap_url', '')
    def current_settings():
        current = config_provider() if config_provider is not None else config
        if current.collectors.get('swap_url', '') != base_url:
            raise WitnessError('configured_endpoint_changed')
        return current.native_witness
    return BoundNativeGenerationReader(base_url, config.native_witness,
        instance_provider=instance_provider, config_reader=config_reader,
        settings_provider=current_settings, clock=clock)
