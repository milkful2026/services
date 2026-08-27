import pytest

from adapters.retry import call_with_retry


def test_negative_max_retries_raises_value_error_instead_of_unbound_local_error():
    with pytest.raises(ValueError, match="max_retries must be >= 0"):
        call_with_retry(
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            max_retries=-1,
            backoff_base_seconds=0.0,
            retryable_exceptions=(RuntimeError,),
        )
