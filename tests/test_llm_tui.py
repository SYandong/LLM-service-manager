# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-6.1-sol
"""CLI fallback remains independent of the optional Textual installation."""

import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize("command", [[], ["top"], ["legacy-tui"]])
@pytest.mark.parametrize("tty,available,launches", [(False, False, False), (False, True, False), (True, False, False), (True, True, True)])
def test_optional_tui_dispatch(monkeypatch, capsys, command, tty, available, launches):
    api = runpy.run_path(str(Path(__file__).resolve().parents[1] / "cli" / "llm"))
    globals_ = api["main"].__globals__
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def request(self, method, path):
            calls.append((method, path))
            return {"schema_version": 1, "services": [], "gpus": [], "stale": True, "config": {}}

    class FakeApp:
        def __init__(self, client, api):
            assert api.build_parser is globals_["build_parser"]

        def run(self):
            calls.append("tui")

    def optional_app():
        assert tty, "Non-TTY must not even import the optional UI"
        return FakeApp if available else None

    monkeypatch.setattr(globals_["sys"].stdout, "isatty", lambda: tty)
    monkeypatch.setitem(globals_, "load_config", lambda **kwargs: {})
    monkeypatch.setitem(globals_, "SchedulerClient", FakeClient)
    monkeypatch.setitem(globals_, "tui_app", optional_app)
    monkeypatch.setitem(globals_, "fleet_tui_app", optional_app)
    assert api["main"](command) == 0
    output = capsys.readouterr()
    fallback = "/v1/state" if command == ["legacy-tui"] else "/v1/fleet"
    assert calls == (["tui"] if launches else [("GET", fallback)])
    assert ("pip install 'llmsvc[tui]'" in output.out) == (not launches)
    assert ("Deprecated: legacy-tui" in output.err) == (command == ["legacy-tui"])


def test_absent_textual_returns_fallback(monkeypatch):
    api = runpy.run_path(str(Path(__file__).resolve().parents[1] / "cli" / "llm"))

    def unavailable(name):
        raise ModuleNotFoundError("No module named 'textual'")

    monkeypatch.setattr(api["importlib"], "import_module", unavailable)
    assert api["tui_app"]() is None
    assert api["fleet_tui_app"]() is None


@pytest.mark.parametrize("command", [["status"], ["fleet"], ["status", "--shared"]])
def test_explicit_read_commands_never_launch_tui(monkeypatch, capsys, command):
    api = runpy.run_path(str(Path(__file__).resolve().parents[1] / "cli" / "llm"))
    globals_ = api["main"].__globals__
    paths = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def request(self, method, path):
            paths.append(path)
            return {"schema_version": 1, "services": [], "gpus": [], "stale": True, "config": {}}

    monkeypatch.setattr(globals_["sys"].stdout, "isatty", lambda: True)
    monkeypatch.setitem(globals_, "load_config", lambda **kwargs: {})
    monkeypatch.setitem(globals_, "SchedulerClient", Client)
    monkeypatch.setitem(globals_, "fleet_tui_app", lambda: pytest.fail("Explicit read commands must stay CLI"))
    assert api["main"](command) == 0
    assert paths == (["/v1/state"] if "--shared" in command else ["/v1/fleet"])
    assert "pip install" not in capsys.readouterr().out
