"""Small, dependency-free runtime primitives for the perception service."""

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time


def utc_iso(epoch=None):
    """Return an epoch as an RFC 3339 UTC timestamp with millisecond precision."""
    if epoch is None:
        epoch = time.time()
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


_URL_OR_QUERY_RE = re.compile(
    r"(?:https?|wss?|rtsp)://|\?|(?:^|[\s,&])(?:sessiontoken|token|signature|"
    r"credential|secret|x-amz-[a-z0-9-]+)\s*=|"
    r"(?:^|[\s,])[a-z0-9_.~-]+=[^\s&]+&[a-z0-9_.~-]+=",
    re.IGNORECASE,
)


def sanitize_source_error(error, max_length=240):
    """Return source diagnostics without URLs, query strings, or credentials.

    Signed HLS session URLs can appear in exception messages raised by HTTP or
    capture libraries.  When a message has any URL/query/credential marker, the
    whole detail is replaced instead of attempting a fragile partial redaction.
    This function is intentionally safe to apply more than once.
    """
    if error is None:
        return None

    is_exception = isinstance(error, BaseException)
    error_type = error.__class__.__name__ if is_exception else ""
    text = " ".join(str(error).split())
    if not text:
        text = "source operation failed"

    if _URL_OR_QUERY_RE.search(text):
        text = "source operation failed (details redacted)"
    elif len(text) > int(max_length):
        text = text[: max(1, int(max_length) - 3)] + "..."

    if is_exception:
        return f"{error_type}: {text}"
    return text


class MonotonicEventClock:
    """Produce trustworthy event times for live streams and recorded media.

    OpenCV's ``CAP_PROP_POS_MSEC`` is a media-relative cursor. It can reset or
    stop advancing after an HLS reconnect, so it must never anchor production
    event time. Live mode uses wall time; recorded mode retains media offsets.
    Returned values are strictly monotonic within a process.
    """

    def __init__(self, live, start_epoch=None, minimum_step_seconds=0.001):
        self.live = bool(live)
        self.start_epoch = time.time() if start_epoch is None else float(start_epoch)
        self.minimum_step_seconds = float(minimum_step_seconds)
        self.last_epoch = None

    def next(self, media_msec=None, now_epoch=None):
        if self.live:
            candidate = time.time() if now_epoch is None else float(now_epoch)
        else:
            if media_msec is None:
                raise ValueError("recorded media requires media_msec")
            candidate = self.start_epoch + max(0.0, float(media_msec)) / 1000.0

        if self.last_epoch is not None and candidate <= self.last_epoch:
            candidate = self.last_epoch + self.minimum_step_seconds
        self.last_epoch = candidate
        return candidate, utc_iso(candidate)


@dataclass
class StreamRecovery:
    """Bounded exponential reconnect state for one live video source."""

    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    failures: int = 0
    next_retry_monotonic: float = 0.0
    last_error: str = None

    def __post_init__(self):
        self.initial_delay_seconds = max(0.1, float(self.initial_delay_seconds))
        self.max_delay_seconds = max(
            self.initial_delay_seconds, float(self.max_delay_seconds)
        )

    def can_retry(self, now_monotonic=None):
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        return now >= self.next_retry_monotonic

    def record_success(self):
        self.failures = 0
        self.next_retry_monotonic = 0.0
        self.last_error = None

    def record_failure(self, error, now_monotonic=None):
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        # Once the delay is capped, do not keep growing an arbitrary-precision
        # integer forever. A source can legitimately remain unavailable for
        # days, and recovery must remain safe for the lifetime of the service.
        exponent = min(self.failures, 62)
        delay = min(
            self.max_delay_seconds,
            self.initial_delay_seconds * (2 ** exponent),
        )
        self.failures += 1
        self.next_retry_monotonic = now + delay
        self.last_error = sanitize_source_error(error)
        return delay


class AttemptRateLimiter:
    """Rate-limit attempts, regardless of whether the attempted work succeeds."""

    def __init__(self, minimum_interval_seconds=0.0):
        self.minimum_interval_seconds = max(
            0.0, float(minimum_interval_seconds)
        )
        self.last_attempt_monotonic = None

    def allow(self, now_monotonic=None):
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        if (
            self.last_attempt_monotonic is not None
            and now - self.last_attempt_monotonic
            < self.minimum_interval_seconds
        ):
            return False
        self.last_attempt_monotonic = now
        return True


def validate_batch_response(payload, expected_count):
    """Reject HTTP-success responses that contain item-level ingest failures."""
    if not isinstance(payload, dict):
        raise ValueError("batch response is not a JSON object")

    expected_count = int(expected_count)
    inserted = payload.get("inserted")
    failed = payload.get("failed")
    results = payload.get("results")
    if payload.get("ok") is not True:
        raise ValueError("batch response reports ok=false")
    if inserted != expected_count or failed != 0:
        raise ValueError(
            f"batch response inserted={inserted!r}, failed={failed!r}, "
            f"expected={expected_count}"
        )
    if not isinstance(results, list) or len(results) != expected_count:
        raise ValueError("batch response result count does not match request")
    if any(not isinstance(item, dict) or item.get("ok") is not True for item in results):
        raise ValueError("batch response contains a failed item")
    return payload
