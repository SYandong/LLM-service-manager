# Generated-By: Codex / gpt-6-astra
"""Known score examples; model size and protection are tested by policy replays."""

import pytest

from llmsvc.policy.ranking import keep_value


def test_keep_value_uses_idle_minutes_and_observed_cold_start():
    assert keep_value(9, 120, 540) == 120
    assert keep_value(9, 240, 540) == 240
    assert keep_value(0, 60, 0) == 60


def test_equal_size_hot_model_is_more_valuable():
    assert keep_value(0, 120, 600) < keep_value(20, 120, 600)


@pytest.mark.parametrize("bad", [None, True, -1, float("nan"), float("inf"), "1"])
@pytest.mark.parametrize("index", [0, 1, 2])
def test_unknown_and_invalid_scores_are_not_silently_ranked(bad, index):
    values = [1, 120, 600]
    values[index] = bad
    with pytest.raises(ValueError):
        keep_value(*values)


def test_request_count_is_integral():
    with pytest.raises(ValueError):
        keep_value(1.5, 120, 600)
