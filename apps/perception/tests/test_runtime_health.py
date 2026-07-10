import sys
from pathlib import Path
import unittest


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PERCEPTION_DIR))

from runtime_health import (  # noqa: E402
    AttemptRateLimiter,
    MonotonicEventClock,
    StreamRecovery,
    sanitize_source_error,
    validate_batch_response,
)


class MonotonicEventClockTests(unittest.TestCase):
    def test_live_clock_uses_wall_time_not_stalled_media_cursor(self):
        clock = MonotonicEventClock(live=True, start_epoch=10.0)
        first_epoch, _ = clock.next(media_msec=500_000, now_epoch=100.0)
        second_epoch, _ = clock.next(media_msec=500_000, now_epoch=101.0)
        self.assertEqual(first_epoch, 100.0)
        self.assertEqual(second_epoch, 101.0)

    def test_clock_remains_strictly_monotonic_during_wall_clock_regression(self):
        clock = MonotonicEventClock(live=True, minimum_step_seconds=0.001)
        first_epoch, _ = clock.next(now_epoch=100.0)
        second_epoch, _ = clock.next(now_epoch=99.0)
        self.assertAlmostEqual(first_epoch, 100.0)
        self.assertAlmostEqual(second_epoch, 100.001)

    def test_recorded_clock_uses_media_offset(self):
        clock = MonotonicEventClock(live=False, start_epoch=1_000.0)
        epoch, _ = clock.next(media_msec=2_500.0)
        self.assertEqual(epoch, 1_002.5)


class StreamRecoveryTests(unittest.TestCase):
    def test_retries_forever_with_bounded_exponential_backoff(self):
        recovery = StreamRecovery(1.0, 4.0)
        delays = [
            recovery.record_failure("read failed", now_monotonic=0.0),
            recovery.record_failure("read failed", now_monotonic=1.0),
            recovery.record_failure("read failed", now_monotonic=3.0),
            recovery.record_failure("read failed", now_monotonic=7.0),
        ]
        self.assertEqual(delays, [1.0, 2.0, 4.0, 4.0])
        self.assertFalse(recovery.can_retry(10.9))
        self.assertTrue(recovery.can_retry(11.0))

    def test_success_resets_backoff(self):
        recovery = StreamRecovery()
        recovery.record_failure("open failed", now_monotonic=5.0)
        recovery.record_success()
        self.assertEqual(recovery.failures, 0)
        self.assertTrue(recovery.can_retry(0.0))
        self.assertIsNone(recovery.last_error)

    def test_long_outage_does_not_overflow_backoff(self):
        recovery = StreamRecovery(1.0, 30.0)
        recovery.failures = 10_000
        self.assertEqual(
            recovery.record_failure("still unavailable", now_monotonic=100.0),
            30.0,
        )
        self.assertEqual(recovery.next_retry_monotonic, 130.0)

    def test_recovery_state_never_retains_a_signed_url(self):
        recovery = StreamRecovery()
        recovery.record_failure(
            "https://video.example/hls?SessionToken=secret-value",
            now_monotonic=1.0,
        )
        self.assertIn("details redacted", recovery.last_error)
        self.assertNotIn("video.example", recovery.last_error)
        self.assertNotIn("secret-value", recovery.last_error)


class BatchResponseTests(unittest.TestCase):
    def test_accepts_only_complete_batch(self):
        payload = {
            "ok": True,
            "inserted": 2,
            "failed": 0,
            "results": [{"ok": True}, {"ok": True}],
        }
        self.assertIs(validate_batch_response(payload, 2), payload)

    def test_rejects_partial_failure_hidden_behind_http_200(self):
        with self.assertRaises(ValueError):
            validate_batch_response(
                {
                    "ok": False,
                    "inserted": 1,
                    "failed": 1,
                    "results": [{"ok": True}, {"ok": False}],
                },
                2,
            )

    def test_rejects_malformed_success(self):
        with self.assertRaises(ValueError):
            validate_batch_response({"ok": True, "inserted": 2, "failed": 0}, 2)


class AttemptRateLimiterTests(unittest.TestCase):
    def test_failed_attempt_still_consumes_the_rate_limit_window(self):
        limiter = AttemptRateLimiter(2.0)
        self.assertTrue(limiter.allow(10.0))
        # The limiter deliberately does not have a success callback: callers
        # cannot accidentally hammer the API after a partial HTTP-200 failure.
        self.assertFalse(limiter.allow(11.999))
        self.assertTrue(limiter.allow(12.0))


class SourceErrorSanitizationTests(unittest.TestCase):
    def test_signed_url_and_query_are_replaced_as_a_whole(self):
        error = RuntimeError(
            "open failed for https://kinesis.example/hls.m3u8?"
            "SessionToken=top-secret&X-Amz-Signature=signature-secret"
        )
        sanitized = sanitize_source_error(error)
        self.assertIn("RuntimeError", sanitized)
        self.assertIn("details redacted", sanitized)
        for forbidden in (
            "https://",
            "kinesis.example",
            "SessionToken",
            "top-secret",
            "X-Amz-Signature",
            "signature-secret",
        ):
            self.assertNotIn(forbidden, sanitized)

    def test_non_sensitive_diagnostic_remains_useful_and_bounded(self):
        self.assertEqual(
            sanitize_source_error(RuntimeError("frame read failed")),
            "RuntimeError: frame read failed",
        )
        sanitized = sanitize_source_error("x" * 500, max_length=40)
        self.assertLessEqual(len(sanitized), 40)

    def test_generic_query_fragment_is_not_exposed(self):
        sanitized = sanitize_source_error("request failed foo=one&bar=two")
        self.assertIn("details redacted", sanitized)
        self.assertNotIn("foo=one", sanitized)
        self.assertNotIn("bar=two", sanitized)


if __name__ == "__main__":
    unittest.main()
