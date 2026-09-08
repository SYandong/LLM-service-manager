# Generated-By: Codex / gpt-6-astra
"""The single retention score used by every ordinary eviction policy."""

from math import isfinite


def keep_value(
    requests_last_hour: int,
    cold_start_seconds: float,
    idle_seconds: float,
) -> float:
    """Return DESIGN §4.1's score for known, non-negative measurements.

    Protection and default-last ordering are separate from this score. Model
    size belongs only in feasibility checks. Callers supply the latest measured
    cold-start duration, falling back to the configured estimate when absent.
    Unknown inputs must be blocked by the caller rather than treated as zero.
    """
    for name, value in (
        ("requests_last_hour", requests_last_hour),
        ("cold_start_seconds", cold_start_seconds),
        ("idle_seconds", idle_seconds),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be a finite non-negative number")
    if not isinstance(requests_last_hour, int):
        raise ValueError("requests_last_hour must be an integer")
    result = (1 + requests_last_hour) * cold_start_seconds / (1 + idle_seconds / 60)
    if not isfinite(result):
        raise ValueError("keep_value must be finite")
    return result
