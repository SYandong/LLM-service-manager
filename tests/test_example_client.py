# Generated-By: Codex / gpt-6-astra
"""Legacy example configuration checks without an SDK or network connection."""
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "test_client.py"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_endpoint_exits_before_sdk_import(value, tmp_path):
    env = dict(os.environ)
    env.pop("LEGACY_OPENAI_BASE_URL", None)
    if value is not None:
        env["LEGACY_OPENAI_BASE_URL"] = value
    # No site packages: importing OpenAI before checking the endpoint would fail
    # with ModuleNotFoundError rather than the required configuration diagnostic.
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(EXAMPLE)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "Set LEGACY_OPENAI_BASE_URL to the administrator-provided legacy OpenAI API base URL"
    )


def test_explicit_endpoint_preserves_client_and_request(monkeypatch, capsys):
    endpoint = "https://legacy.example.invalid/v1"
    monkeypatch.setenv("LEGACY_OPENAI_BASE_URL", endpoint)
    calls = []

    def create(**kwargs):
        calls.append(("request", kwargs))
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="stub response"),
        )])

    def client(**kwargs):
        calls.append(("client", kwargs))
        return SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create),
        ))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=client))
    runpy.run_path(str(EXAMPLE), run_name="__main__")
    assert calls == [
        ("client", {"base_url": endpoint, "api_key": "unused"}),
        ("request", {
            "model": "Qwen/Qwen3-4B-Instruct-2507",
            "messages": [{"role": "user", "content": "Who are you?"}],
        }),
    ]
    assert capsys.readouterr().out == "stub response\n"
