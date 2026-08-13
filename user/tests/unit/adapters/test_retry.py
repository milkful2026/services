import pytest

from adapters.retry import call_with_retry


class _RetryableError(Exception):
    pass


class _OtherError(Exception):
    pass


def test_returns_result_on_first_success():
    result = call_with_retry(
        lambda: "ok", max_retries=2, backoff_base_seconds=0, retryable_exceptions=(_RetryableError,)
    )
    assert result == "ok"


def test_retries_on_retryable_exception_then_succeeds():
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _RetryableError("transient")
        return "ok"

    result = call_with_retry(
        _fn, max_retries=2, backoff_base_seconds=0, retryable_exceptions=(_RetryableError,)
    )

    assert result == "ok"
    assert calls["n"] == 3


def test_raises_final_exception_after_exhausting_retries():
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        raise _RetryableError(f"attempt {calls['n']}")

    with pytest.raises(_RetryableError, match="attempt 3"):
        call_with_retry(
            _fn, max_retries=2, backoff_base_seconds=0, retryable_exceptions=(_RetryableError,)
        )

    assert calls["n"] == 3  # initial attempt + 2 retries


def test_non_retryable_exception_propagates_immediately_without_retrying():
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        raise _OtherError("not retryable")

    with pytest.raises(_OtherError):
        call_with_retry(
            _fn, max_retries=2, backoff_base_seconds=0, retryable_exceptions=(_RetryableError,)
        )

    assert calls["n"] == 1


def test_on_attempt_failure_callback_invoked_per_retryable_failure():
    seen: list[tuple[str, int]] = []

    def _fn():
        raise _RetryableError("boom")

    with pytest.raises(_RetryableError):
        call_with_retry(
            _fn,
            max_retries=2,
            backoff_base_seconds=0,
            retryable_exceptions=(_RetryableError,),
            on_attempt_failure=lambda exc, attempt: seen.append((str(exc), attempt)),
        )

    assert seen == [("boom", 0), ("boom", 1), ("boom", 2)]
