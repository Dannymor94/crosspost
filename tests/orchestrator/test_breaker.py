"""Policy validation and HTTP Retry-After parsing; dispatch proof is separate."""

from datetime import UTC, datetime

import pytest

from crosspost.orchestrator.breaker import BreakerPolicy, retry_after_seconds, utc_naive


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "broken", None])
def test_invalid_policy_fails_safe(value, monkeypatch):
    if value is not None:
        monkeypatch.setenv("BREAKER_COOLDOWN_SECONDS", value)
        monkeypatch.setenv("BREAKER_LEASE_SECONDS", value)
    else:
        monkeypatch.delenv("BREAKER_COOLDOWN_SECONDS", raising=False)
        monkeypatch.delenv("BREAKER_LEASE_SECONDS", raising=False)
    assert BreakerPolicy.from_env() == BreakerPolicy(300, 600)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("10", 10),
        ("nan", None),
        ("-1", None),
        ("bad", None),
        ("999999999", 86400),
        ("Thu, 01 Jan 2026 00:00:30 GMT", 30),
    ],
)
def test_retry_after(value, expected):
    assert retry_after_seconds(value, now=datetime(2026, 1, 1, tzinfo=UTC)) == expected


def test_aware_time_is_converted_to_utc():
    assert utc_naive(datetime.fromisoformat("2026-01-01T03:00:00+03:00")) == datetime(2026, 1, 1)
