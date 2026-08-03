import asyncio
import logging
import traceback
from collections.abc import Mapping

import requests
import tenacity

# Substrings of transient backend failures worth retrying (gateway hiccups, rate
# limits, and the "no available channel" messages returned under provider load).
_TRANSIENT_MARKERS = (
    "503",
    "502",
    "504",
    "429",
    "timeout",
    "timed out",
    "connection error",
    "connection reset",
    "temporar",
    "rate limit",
    "overload",
    "try again",
    "no available channel",
    "\u65e0\u53ef\u7528\u6e20\u9053",
    "\u4e0a\u6e38\u8d1f\u8f7d\u5df2\u9971\u548c",
    "\u8bf7\u7a0d\u540e\u518d\u8bd5",
)


def is_retryable_http_status(status: int) -> bool:
    """Return whether an HTTP response can reasonably succeed on a later try."""
    return int(status) == 429 or int(status) >= 500


def retry_after_seconds(
    headers: Mapping[str, object] | None,
    attempt: int,
    *,
    base_wait: float = 5.0,
    max_wait: float = 30.0,
) -> float:
    """Honor a numeric Retry-After header, otherwise use bounded backoff."""
    fallback = min(max_wait, base_wait * (2 ** max(0, int(attempt) - 1)))
    if not headers:
        return fallback
    raw = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return min(max_wait, max(0.0, float(str(raw).strip())))
    except (TypeError, ValueError):
        return fallback


def after_func(retry_state: tenacity.RetryCallState) -> None:
    if retry_state.outcome.failed:
        exc = retry_state.outcome.exception()
        logging.warning(
            f"Retrying {retry_state.fn.__name__} due to {repr(exc)} "
            f"(Attempt {retry_state.attempt_number})"
        )
        logging.debug(traceback.format_exception(type(exc), exc, exc.__traceback__))


# Shared retry for LLM structured-output calls (storyboard / character extraction /
# portraits). Adds exponential backoff + jitter so a few-second gateway or network
# blip is ridden out. ``reraise`` surfaces the underlying error rather than a
# tenacity RetryError, keeping the job's failure note meaningful.
llm_retry = tenacity.retry(
    stop=tenacity.stop_after_attempt(4),
    wait=tenacity.wait_exponential(multiplier=1.5, max=20) + tenacity.wait_random(0, 1),
    after=after_func,
    reraise=True,
)


def is_retryable_download_error(exc: BaseException) -> bool:
    """Retry network and server errors, but fail fast on deterministic 4xx errors."""
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        return response is None or response.status_code >= 500
    return isinstance(exc, requests.RequestException)


download_retry = tenacity.retry(
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=1, max=10),
    retry=tenacity.retry_if_exception(is_retryable_download_error),
    after=after_func,
    reraise=True,
)


def is_retryable_generation_error(exc: BaseException) -> bool:
    """Classify transient generation failures without retrying bad input or auth."""
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        return response is None or is_retryable_http_status(response.status_code)
    if isinstance(exc, requests.RequestException):
        return True
    return any(marker in str(exc).lower() for marker in _TRANSIENT_MARKERS)


async def retry_async(
    factory,
    attempts: int = 3,
    base_wait: float = 2.0,
    max_wait: float = 30.0,
    label: str = "generation",
):
    """Run an async factory with bounded backoff for transient failures."""
    last = None
    for i in range(1, max(1, attempts) + 1):
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001 - non-transient errors re-raise below
            last = exc
            if i >= attempts or not is_retryable_generation_error(exc):
                raise
            wait = min(max_wait, base_wait * (2 ** (i - 1)))
            logging.warning(
                "Retrying %s after transient error (attempt %d/%d): %r",
                label,
                i,
                attempts,
                exc,
            )
            await asyncio.sleep(wait)
    if last is not None:
        raise last
