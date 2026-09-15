"""Scheduler configuration keys that build the shared PolicySettings."""

import pytest

from llmsvc.config import SchedulerConfig, load_config
from llmsvc.policy import PolicySettings


def config(**kwargs):
    return SchedulerConfig("127.0.0.1", 19001, **kwargs)


def test_defaults_match_policy_defaults():
    assert config().policy_settings() == PolicySettings()


def test_pool_fit_exclusive_and_threshold_flow_into_policy_settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("listen_host: 127.0.0.1\nlisten_port: 19001\n"
                    "exclusive_gpu: 0\nplacement_gpus: [0, 1, 2, 3]\nplacement_fit: best_fit\n"
                    "shared_external_threshold_gb: 30\n")
    loaded = load_config(str(path))
    settings = loaded.policy_settings()
    assert settings.placement_gpus == (0, 1, 2, 3)
    assert settings.placement_fit == "best_fit"
    assert settings.exclusive_gpu == 0
    assert settings.shared_external_threshold_gb == 30
    # The gpu_pressure automation threshold is a separate knob and stays unchanged.
    assert loaded.automation_shared_external_threshold_gb == 1.0


def test_exclusive_gpu_can_move_with_the_pool():
    settings = config(exclusive_gpu=2, placement_gpus=[2, 3]).policy_settings()
    assert settings.exclusive_gpu == 2 and settings.placement_gpus == (2, 3)


def test_maintenance_timeout_defaults_and_accepts_bounded_yaml(tmp_path):
    assert config().maintenance_timeout_seconds == 300.0
    path = tmp_path / "config.yaml"
    path.write_text("listen_host: 127.0.0.1\nlisten_port: 19001\nmaintenance_timeout_seconds: 120\n")
    assert load_config(str(path)).maintenance_timeout_seconds == 120


@pytest.mark.parametrize("value", ["1200", "true", "0", "-1", "nan", "900.5"])
def test_maintenance_timeout_rejects_out_of_range_or_boolean(tmp_path, value):
    path = tmp_path / "config.yaml"
    path.write_text(f"listen_host: 127.0.0.1\nlisten_port: 19001\nmaintenance_timeout_seconds: {value}\n")
    with pytest.raises(ValueError):
        load_config(str(path))


@pytest.mark.parametrize("kwargs", [
    {"placement_gpus": []}, {"placement_gpus": [0, 0]}, {"placement_gpus": [1]}, {"placement_gpus": [0, "1"]},
    {"placement_gpus": [0, True]}, {"placement_gpus": (0, 1)}, {"exclusive_gpu": -1}, {"exclusive_gpu": True},
    {"exclusive_gpu": 1.0}, {"placement_fit": "worst_fit"}, {"shared_external_threshold_gb": -1},
    {"shared_external_threshold_gb": float("nan")}, {"shared_external_threshold_gb": True},
])
def test_invalid_policy_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        config(**kwargs)
