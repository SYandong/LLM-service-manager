# Generated-By: Codex / gpt-6-astra
"""CLI fallback remains independent of the optional Textual installation."""

import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize("tty,available,launches", [(False, False, False), (False, True, False), (True, False, False), (True, True, True)])
def test_optional_tui_dispatch(monkeypatch, capsys, tty, available, launches):
    api = runpy.run_path(str(Path(__file__).resolve().parents[1] / "cli" / "llm"))
    globals_ = api["main"].__globals__
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def request(self, method, path):
            calls.append((method, path))
            return {}

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
    assert api["main"]([]) == 0
    output = capsys.readouterr().out
    assert calls == (["tui"] if launches else [("GET", "/v1/state")])
    assert ("pip install 'llmsvc[tui]'" in output) == (not launches)


def test_absent_textual_returns_fallback(monkeypatch):
    api = runpy.run_path(str(Path(__file__).resolve().parents[1] / "cli" / "llm"))

    def unavailable(name):
        raise ModuleNotFoundError("No module named 'textual'")

    monkeypatch.setattr(api["importlib"], "import_module", unavailable)
    assert api["tui_app"]() is None
