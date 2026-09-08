# Generated-By: Codex / gpt-6-astra
"""Durability and zero-write intent preview checks using disposable databases."""

import sqlite3
import threading

import pytest

from llmsvc.state import Pin, Reserve
from llmsvc.store import IntentStore


def test_pin_and_reserve_survive_restart_and_expire_without_deletion(tmp_path):
    path = tmp_path / "state.sqlite"
    lock = threading.RLock()
    store = IntentStore(path, action_lock=lock)
    store.put_pin(Pin("model", 200, "owner"))
    store.put_pin(Pin("model", 300, "new-owner"))
    store.put_reserve(Reserve("reservation", 1, 80, 250, "owner"))
    store.close()
    before = path.read_bytes()
    store = IntentStore(path, action_lock=lock, read_only=True)
    assert store.active(100) == ((Pin("model", 300, "new-owner"),), (Reserve("reservation", 1, 80, 250, "owner"),))
    assert store.active(250)[1] == ()
    assert store.active(300) == ((), ())
    store.close()
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM llmsvc_pins").fetchone() == (1,)


def test_dry_run_invokes_no_writer_and_changes_no_files(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite"
    store = IntentStore(path, action_lock=threading.RLock())
    before = path.read_bytes()
    def fail(*args):
        raise AssertionError("writer invoked in dry-run")
    monkeypatch.setattr(store, "_write", fail)
    assert store.put_pin(Pin("model", 200, "owner"), dry_run=True)["would"]
    assert store.remove_pin("model", dry_run=True)["would"]
    assert store.put_reserve(Reserve("r", 0, 80, 200, "owner"), dry_run=True)["would"]
    assert store.remove_reserve("r", dry_run=True)["would"]
    assert store.active(100) == ((), ())
    store.close()
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_readonly_missing_database_does_not_create_file(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        IntentStore(path, action_lock=threading.RLock(), read_only=True)
    assert not path.exists()


def test_readonly_store_rejects_real_writes(tmp_path):
    path = tmp_path / "state.sqlite"
    lock = threading.RLock()
    IntentStore(path, action_lock=lock).close()
    store = IntentStore(path, action_lock=lock, read_only=True)
    with pytest.raises(PermissionError):
        store.put_pin(Pin("model", 200, "owner"))
    store.close()


def test_sql_values_are_parameterized_and_deletes_idempotent(tmp_path):
    store = IntentStore(tmp_path / "state.sqlite", action_lock=threading.RLock())
    name = "model'; DROP TABLE llmsvc_pins; --"
    store.put_pin(Pin(name, 200, "owner"))
    assert store.active(100)[0][0].model == name
    store.remove_pin(name)
    store.remove_pin(name)
    assert store.active(100) == ((), ())
    store.close()


@pytest.mark.parametrize("pin", [Pin("", 200, "owner"), Pin("m", float("nan"), "owner"), Pin("m", 200, "")])
def test_invalid_pin_does_not_write(tmp_path, pin):
    store = IntentStore(tmp_path / "state.sqlite", action_lock=threading.RLock())
    with pytest.raises(ValueError):
        store.put_pin(pin)
    assert not store.active(100)[0]
    store.close()


def test_concurrent_pin_updates_have_one_durable_model_record(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = IntentStore(tmp_path / "state.sqlite", action_lock=threading.RLock())
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(store.put_pin, Pin("model", 200 + i, f"owner-{i}")) for i in range(12)]
        for future in futures:
            future.result()
    assert len(store.active(100)[0]) == 1
    store.close()
