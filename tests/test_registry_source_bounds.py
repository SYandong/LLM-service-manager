# Generated-By: Codex / gpt-6-astra
"""Bounded source reads through the real core preview mount (temporary files only)."""

import os

import pytest

from llmsvc.registry import ModelRegistry, validate_full_weight_model_dir
from llmsvc.reload import MAX_SOURCE_BYTES, ReloadError, ReloadQueue, _read_regular_file
from test_registry_http_preview import assert_readonly, mounted, registry_fixture, request


def preview(mounted):
    return request(mounted.address, "POST", "/v1/models?dry_run=1",
                   {"name": "candidate", "path": str(mounted.weights), "base": "base"})


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1.5, float("inf"), None, "1024", MAX_SOURCE_BYTES + 1])
def test_constructor_caps_cannot_disable_bounds(registry_fixture, bad):
    registry, queue, *_ = registry_fixture
    with pytest.raises(ValueError, match="config_max_bytes"):
        ReloadQueue(queue.path, action_lock=queue.action_lock, quiet=queue.quiet,
                    snapshot=queue.snapshot, validate=queue.validate, notify_reload=queue.notify_reload,
                    log=queue.log, config_max_bytes=bad)
    for field in ("model_config_max_bytes", "weight_index_max_bytes"):
        with pytest.raises(ValueError, match=field):
            ModelRegistry(queue, shared_roots=registry.shared_roots,
                          daemon_port_range=registry.daemon_port_range, **{field: bad})


def test_config_exact_limit_then_one_byte_over_is_503_for_list_and_preview(mounted):
    before = mounted.files(), mounted.scheduler.events_since(0)
    mounted.registry.queue.config_max_bytes = mounted.path.stat().st_size
    assert request(mounted.address, "GET", "/v1/models")[0] == 200
    assert preview(mounted)[0] == 200
    mounted.registry.queue.config_max_bytes -= 1
    assert request(mounted.address, "GET", "/v1/models") == (503, {"error": "registry_unavailable"})
    assert preview(mounted) == (503, {"error": "registry_unavailable"})
    assert_readonly(mounted, before)


@pytest.mark.parametrize("kind", ["config", "index"])
def test_metadata_exact_limit_then_one_byte_over_is_request_400(mounted, kind):
    if kind == "config":
        target = mounted.weights / "config.json"
        field = "model_config_max_bytes"
    else:
        target = mounted.weights / "model.safetensors.index.json"
        target.write_text('{"weight_map":{"layer":"model.safetensors"}}')
        field = "weight_index_max_bytes"
    setattr(mounted.registry, field, target.stat().st_size)
    before = mounted.files(), mounted.scheduler.events_since(0)
    assert preview(mounted)[0] == 200
    setattr(mounted.registry, field, target.stat().st_size - 1)
    status, body = preview(mounted)
    assert status == 400 and body["error"] == "registry_invalid_request"
    assert "oversized" in body["message"]
    assert_readonly(mounted, before)


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "hardlink", "parent-symlink"])
def test_unsafe_config_sources_fail_without_blocking_or_writing(mounted, monkeypatch, kind):
    path = mounted.path
    raw = path.read_bytes()
    if kind == "parent-symlink":
        link = path.parent / "linked-parent"
        link.symlink_to(path.parent, target_is_directory=True)
        mounted.registry.queue.path = link / path.name
    else:
        path.unlink()
        if kind == "fifo":
            os.mkfifo(path)
        elif kind == "directory":
            path.mkdir()
        else:
            backup = path.with_suffix(".backup")
            backup.write_bytes(raw)
            if kind == "symlink":
                path.symlink_to(backup)
            else:
                os.link(backup, path)
    original_open = os.open
    def checked_open(path, flags, *args, **kwargs):
        # Fail before opening a FIFO if the protection regresses; no wall-clock race.
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr("llmsvc.reload.os.open", checked_open)
    # The standard files() helper follows parent symlinks; avoid it in that case.
    before = None if kind == "parent-symlink" else (mounted.files(), mounted.scheduler.events_since(0))
    assert request(mounted.address, "GET", "/v1/models") == (503, {"error": "registry_unavailable"})
    assert preview(mounted) == (503, {"error": "registry_unavailable"})
    if before is not None:
        assert_readonly(mounted, before)
    else:
        assert path.read_bytes() == raw
        assert not mounted.registry.queue._jobs and not mounted.registry.queue._pending


@pytest.mark.parametrize("filename", ["config.json", "model.safetensors.index.json", "model.safetensors"])
@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_model_special_files_are_rejected_as_request_errors(mounted, monkeypatch, filename, kind):
    path = mounted.weights / filename
    path.unlink(missing_ok=True)
    if kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    original_open = os.open
    def checked_open(path, flags, *args, **kwargs):
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr("llmsvc.reload.os.open", checked_open)
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, body = preview(mounted)
    assert status == 400 and body["error"] == "registry_invalid_request"
    assert_readonly(mounted, before)


@pytest.mark.parametrize("content", [b'\xff', b'{"bad":', b'[' * 1500 + b']' * 1500])
@pytest.mark.parametrize("filename", ["config.json", "model.safetensors.index.json"])
def test_malformed_model_json_stays_request_400(mounted, filename, content):
    (mounted.weights / filename).write_bytes(content)
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, body = preview(mounted)
    assert status == 400 and body["error"] == "registry_invalid_request"
    assert_readonly(mounted, before)


def test_confined_model_links_and_large_weights_keep_existing_semantics(mounted):
    shared = mounted.weights.parent
    for filename in ("config.json", "model.safetensors"):
        path = mounted.weights / filename
        cached = shared / (filename + ".cached")
        path.rename(cached)
        path.symlink_to(cached)
    # Sparse fixture: do not allocate/read actual model-sized bytes.
    with (shared / "model.safetensors.cached").open("r+b") as stream:
        stream.truncate(MAX_SOURCE_BYTES * 2)
    model_alias = shared / "model-alias"
    model_alias.symlink_to(mounted.weights, target_is_directory=True)
    info = validate_full_weight_model_dir(model_alias, (shared,))
    assert info.weight_files == ("model.safetensors",)
    assert info.path == str(mounted.weights)
    assert preview(mounted)[0] == 200
    assert not mounted.registry.queue._jobs and not mounted.registry.queue._pending


@pytest.mark.parametrize("change", ["grow", "truncate", "replace", "unlink"])
def test_regular_read_rejects_detectable_races_with_bounded_allocation(tmp_path, monkeypatch, change):
    path = tmp_path / "source"
    path.write_bytes(b"12345678")
    original_stat, original_fdopen = os.fstat, os.fdopen
    stats, reads = [], []
    def racing_stat(fd):
        result = original_stat(fd)
        stats.append(result)
        if len(stats) == 1:
            if change == "grow":
                path.write_bytes(b"123456789")
            elif change == "truncate":
                path.write_bytes(b"1")
            elif change == "replace":
                replacement = tmp_path / "new"
                replacement.write_bytes(b"abcdefgh")
                replacement.replace(path)
            else:
                path.unlink()
        return result
    class BoundedStream:
        def __init__(self, *args, **kwargs):
            self.stream = original_fdopen(*args, **kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            reads.append(size)
            assert size == 9
            return self.stream.read(size)
    monkeypatch.setattr("llmsvc.reload.os.fstat", racing_stat)
    monkeypatch.setattr("llmsvc.reload.os.fdopen", BoundedStream)
    with pytest.raises((ReloadError, OSError)):
        _read_regular_file(path, 8)
    assert reads == [9]


def test_oversized_source_rejects_before_read(tmp_path, monkeypatch):
    path = tmp_path / "large"
    path.write_bytes(b"123456789")
    original_fdopen = os.fdopen
    class NoRead:
        def __init__(self, *args, **kwargs):
            self.stream = original_fdopen(*args, **kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, *args):
            pytest.fail("Oversized source must be rejected before allocating its contents")
    monkeypatch.setattr("llmsvc.reload.os.fdopen", NoRead)
    with pytest.raises(ReloadError, match="byte limit"):
        _read_regular_file(path, 8)


def test_alias_collision_is_still_rejected_by_actual_http(mounted):
    before = mounted.files(), mounted.scheduler.events_since(0)
    status, body = request(mounted.address, "POST", "/v1/models?dry_run=1",
                           {"name": "default", "path": str(mounted.weights), "base": "base"})
    assert status == 400 and body["error"] == "registry_invalid_request"
    assert "already exists" in body["message"]
    assert_readonly(mounted, before)


def test_symlink_loop_is_request_400_and_does_not_read_target(mounted):
    (mounted.weights / "loop").symlink_to("loop")
    status, body = preview(mounted)
    assert status == 400 and body["error"] == "registry_invalid_request"
    assert not mounted.registry.queue._jobs and not mounted.registry.queue._pending
