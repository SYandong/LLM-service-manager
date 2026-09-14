# Generated-By: Claude Code / claude-fable-5-1
"""Cloning a model block whose thin launcher carries options after the unit token."""

import shlex

import pytest

from llmsvc.registry import RegistryError, _transform_cmd

LIVE = (
    "/opt/llama-swap/vllm-wrapper serve --vllm-url http://127.0.0.1:8106 --listen :5804 "
    "--wait-timeout 10m --journal-unit vllm-qwen2.5-7b-instruct.service -- "
    "/opt/rev/deploy/vllm-launch 0.20 vllm-qwen2.5-7b-instruct --config /etc/llmsvc/launcher.json -- "
    "/opt/vllm-0.28.0/bin/vllm serve /srv/models/Qwen2.5-7B-Instruct --host 127.0.0.1 "
    "--served-model-name qwen2.5-7b-instruct --enable-sleep-mode --max-num-seqs 8 "
    "--gpu-memory-utilization 0.20 --port 8106 --max-model-len 32768"
)


def test_launcher_options_after_the_unit_are_kept_and_unit_tokens_renamed():
    result = _transform_cmd(shlex.split(LIVE), "qwen2.5-7b-instruct", "qwen25-import-test",
                            "/srv/models/qwen25-import-test", 8108)
    first, second = [i for i, t in enumerate(result) if t == "--"]
    assert result[first + 1:second] == ["/opt/rev/deploy/vllm-launch", "0.20", "vllm-qwen25-import-test",
                                        "--config", "/etc/llmsvc/launcher.json"]
    assert "--journal-unit" in result and result[result.index("--journal-unit") + 1] == "vllm-qwen25-import-test.service"
    assert result[result.index("--vllm-url") + 1] == "http://127.0.0.1:8108"
    assert result[result.index("--port") + 1] == "8108"
    assert result[result.index("--served-model-name") + 1] == "qwen25-import-test"
    assert result[result.index("serve", second) + 1] == "/srv/models/qwen25-import-test"


@pytest.mark.parametrize("extra", ["vllm-other", "--", "vllm-qwen2.5-7b-instruct"])
def test_launcher_options_cannot_smuggle_a_second_unit_or_delimiter(extra):
    argv = shlex.split(LIVE)
    first = argv.index("--")
    argv.insert(first + 4, extra)
    with pytest.raises(RegistryError):
        _transform_cmd(argv, "qwen2.5-7b-instruct", "x-model", "/srv/models/x", 8108)


def test_three_token_launch_segment_still_works():
    argv = shlex.split(LIVE.replace(" --config /etc/llmsvc/launcher.json", ""))
    result = _transform_cmd(argv, "qwen2.5-7b-instruct", "x-model", "/srv/models/x", 8108)
    first, second = [i for i, t in enumerate(result) if t == "--"]
    assert result[first + 1:second] == ["/opt/rev/deploy/vllm-launch", "0.20", "vllm-x-model"]
