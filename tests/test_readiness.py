from unittest.mock import patch, MagicMock
from vllm_service.readiness import is_ready, wait_until_ready


def test_is_ready_success():
    with patch("urllib.request.urlopen", return_value=MagicMock()):
        assert is_ready("127.0.0.1", 8000) is True


def test_is_ready_failure():
    with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
        assert is_ready("127.0.0.1", 8000) is False


def test_wait_until_ready_succeeds_immediately():
    with patch("vllm_service.readiness.is_ready", return_value=True):
        assert wait_until_ready("127.0.0.1", 8000, timeout=10) is True


def test_wait_until_ready_times_out():
    with patch("vllm_service.readiness.is_ready", return_value=False):
        with patch("time.sleep"):
            assert wait_until_ready("127.0.0.1", 8000, timeout=1) is False


def test_wait_until_ready_stops_when_process_dies():
    with patch("vllm_service.readiness.is_ready", return_value=False):
        with patch("time.sleep"):
            assert wait_until_ready("127.0.0.1", 8000, timeout=60, alive_fn=lambda: False) is False
