# Generated-By: Codex / gpt-6-astra
import importlib.util
import json
import sys
import os
import signal
import pytest

pytestmark = pytest.mark.skipif(not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"), reason="requires a Linux pidfd-enabled Python build; target Python 3.10 is verified")
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "reload_smoke.py"


def load_reload_smoke():
    spec = importlib.util.spec_from_file_location("deploy_reload_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def output_path(stdout):
    return Path(json.loads(stdout)["output"])


def test_dry_run_validates_without_file_or_loopback_mutation(tmp_path, capsys):
    smoke = load_reload_smoke()
    out = tmp_path / "out"

    assert smoke.main(["--dry-run", "--output-dir", str(out), "--llama-swap-binary", str(tmp_path / "missing")]) == 0

    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True
    assert plan["mock_vs_real"]["mock_upstream"] == "not_executed"
    assert plan["mock_vs_real"]["real_llama_swap"]["status"] == "not_executed"
    assert not out.exists()


def test_fake_reload_records_unique_port_timestamps_and_cleanup(tmp_path, capsys):
    smoke = load_reload_smoke()
    args = ["--output-dir", str(tmp_path / "out"), "--request-count", "3", "--chunks", "4",
            "--chunk-delay-seconds", "0.01", "--reload-after-seconds", "0.02"]

    assert smoke.main(args) == 0
    first = json.loads(output_path(capsys.readouterr().out).read_text(encoding="utf-8"))
    assert smoke.main(args) == 0
    second = json.loads(output_path(capsys.readouterr().out).read_text(encoding="utf-8"))

    assert first["run_id"] != second["run_id"]
    assert first["resources"]["upstream_port"] != 0
    assert second["resources"]["upstream_port"] != 0
    assert not Path(first["resources"]["tmpdir"]).exists()
    assert not Path(second["resources"]["tmpdir"]).exists()
    for key in ("last_safe_check_at", "renamed_at", "triggered_at", "adopted_at"):
        assert key in first["reload_timestamps"]
    assert first["scope"]["production_endpoint"] is False
    assert first["scope"]["certifies_v252_continuous_quiet"] is False


def test_abort_mode_classifies_interrupted_streams(tmp_path, capsys):
    smoke = load_reload_smoke()

    assert smoke.main(["--output-dir", str(tmp_path / "out"), "--request-count", "5", "--chunks", "20",
                       "--chunk-delay-seconds", "0.02", "--reload-after-seconds", "0.04",
                       "--mode", "abort"]) == 0

    evidence = json.loads(output_path(capsys.readouterr().out).read_text(encoding="utf-8"))
    assert evidence["request_summary"]["truncated_stream"] >= 1
    assert evidence["mock_vs_real"]["real_llama_swap"]["status"] == "not_configured"


def test_wait_mode_lets_started_streams_complete(tmp_path, capsys):
    smoke = load_reload_smoke()

    assert smoke.main(["--output-dir", str(tmp_path / "out"), "--request-count", "4", "--chunks", "3",
                       "--chunk-delay-seconds", "0.01", "--reload-after-seconds", "0.01",
                       "--mode", "wait"]) == 0

    evidence = json.loads(output_path(capsys.readouterr().out).read_text(encoding="utf-8"))
    assert evidence["reload_timestamps"]["mode"] == "wait"
    assert evidence["request_summary"]["completed"] == 4
    assert evidence["request_summary"]["truncated_stream"] == 0


def test_partial_upstream_stream_is_not_counted_as_completed(tmp_path, capsys):
    smoke = load_reload_smoke()

    assert smoke.main(["--output-dir", str(tmp_path / "out"), "--request-count", "2", "--chunks", "5",
                       "--chunk-delay-seconds", "0.01", "--reload-after-seconds", "0.2",
                       "--fail-after-chunks", "1"]) == 0

    evidence = json.loads(output_path(capsys.readouterr().out).read_text(encoding="utf-8"))
    assert evidence["request_summary"]["completed"] == 0
    assert evidence["request_summary"]["truncated_stream"] == 2


def test_failed_startup_cleans_owned_tempdir(tmp_path, monkeypatch):
    smoke = load_reload_smoke()
    created = []
    real_mkdtemp = smoke.tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = Path(real_mkdtemp(*args, **kwargs))
        created.append(path)
        return str(path)

    monkeypatch.setattr(smoke.tempfile, "mkdtemp", tracking_mkdtemp)
    monkeypatch.setattr(smoke, "reserve_loopback_server",
                        lambda state: (_ for _ in ()).throw(OSError("bind failed")))

    try:
        smoke.run_smoke(smoke.merged_settings(smoke.parse_args(["--output-dir", str(tmp_path / "out")])),
                        tmp_path / "out")
    except OSError:
        pass
    else:
        raise AssertionError("startup failure should propagate")
    assert created and all(not path.exists() for path in created)


def test_bad_config_rejected_before_output(tmp_path):
    smoke = load_reload_smoke()
    config = tmp_path / "bad.json"
    config.write_text(json.dumps({"request_count": 0}), encoding="utf-8")

    assert smoke.main(["--config", str(config), "--output-dir", str(tmp_path / "out")]) == 2
    assert not (tmp_path / "out").exists()


def test_hard_deadline_kills_owned_worker_and_preserves_foreign_files(tmp_path, capsys):
    import time
    smoke=load_reload_smoke()
    output=tmp_path/"output";output.mkdir();foreign=output/"foreign.txt";foreign.write_text("keep")
    began=time.monotonic()
    assert smoke.main(["--output-dir",str(output),"--deadline-seconds","0.5","--mode","wait", "--chunks","1000","--chunk-delay-seconds","1","--reload-after-seconds","0.05"]) == 2
    result=json.loads(capsys.readouterr().out)
    evidence=json.loads(Path(result["output"]).read_text())
    assert evidence["status"] == "deadline_exceeded"
    assert evidence["cleanup"] == {"owned_process_exited": True,"owned_tmpdir_removed":True}
    assert time.monotonic()-began < 2
    assert foreign.read_text()=="keep"


def test_nonfinite_deadlines_and_resource_explosion_rejected(tmp_path):
    smoke=load_reload_smoke()
    for setting in ({"deadline_seconds":float("nan")},{"deadline_seconds":float("inf")},{"request_count":100000}):
        config=tmp_path/"bad.json";config.write_text(json.dumps(setting))
        assert smoke.main(["--config",str(config),"--output-dir",str(tmp_path/"output")])==2
        assert not (tmp_path/"output").exists()


def test_concurrent_reserved_ports_are_distinct():
    smoke=load_reload_smoke();states=[smoke.UpstreamState(1,.01),smoke.UpstreamState(1,.01)]
    servers=[]
    try:
        for state in states:servers.append(smoke.reserve_loopback_server(state))
        assert servers[0][2] != servers[1][2]
    finally:
        for state in states:state.stopping.set()
        for server,thread,_port in servers:server.shutdown();server.server_close();thread.join(timeout=1)


def test_cleanup_kills_only_owned_session_including_new_process_groups():
    import subprocess,time,os,signal
    smoke=load_reload_smoke()
    code="import os,subprocess,sys,time;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'],preexec_fn=os.setpgrp);print(p.pid,flush=True);time.sleep(10)"
    process=subprocess.Popen([sys.executable,"-c",code],stdout=subprocess.PIPE,text=True,start_new_session=True)
    try:
        child=int(process.stdout.readline())
        assert os.getsid(child)==process.pid
        smoke.kill_owned_session(process.pid)
        process.wait(timeout=2)
        until=time.monotonic()+1
        while smoke.owned_session_members(process.pid) and time.monotonic()<until:time.sleep(.01)
        assert not smoke.owned_session_members(process.pid)
        assert os.getpid()!=process.pid
    finally:
        if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=2)
        process.stdout.close()
