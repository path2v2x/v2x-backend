"""Thread-isolated capture for live HLS sources.

OpenCV/FFmpeg reads can block until their configured timeout.  One worker per
camera keeps an unhealthy HLS source from delaying every healthy camera in the
perception pipeline.  The worker retains only the newest frame and assigns a
sequence number so consumers never mistake a cached frame for new input.
"""

from collections import deque
import hashlib
import math
import threading
import time

from runtime_health import sanitize_source_error


class _ProactiveRenewal(Exception):
    """Internal control flow for rotating a signed source before expiry."""


def _bounded_bytes(value, limit=32_768):
    """Return a bounded byte representation without copying a full frame."""
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, bytearray):
        raw = bytes(value)
    elif isinstance(value, memoryview):
        raw = value.tobytes()
    elif isinstance(value, str):
        raw = value.encode("utf-8", errors="replace")
    else:
        raw = repr(value).encode("utf-8", errors="replace")

    if len(raw) <= limit:
        return raw
    stride = max(1, math.ceil(len(raw) / limit))
    return raw[::stride][:limit]


def bounded_frame_identity(frame, axis_samples=64):
    """Fingerprint bounded, spatially distributed frame content.

    A full 2560x1920 BGR hash on every decoded frame would add substantial CPU
    and memory bandwidth.  This samples at most roughly ``axis_samples ** 2``
    pixels plus five small anchor patches and hashes only that bounded payload.
    Shape and dtype are part of the identity.
    """
    digest = hashlib.blake2b(digest_size=16)
    shape = tuple(getattr(frame, "shape", ()) or ())
    dtype = str(getattr(frame, "dtype", ""))
    digest.update(repr((shape, dtype)).encode("ascii", errors="replace"))

    if len(shape) >= 2 and hasattr(frame, "__getitem__"):
        height = max(1, int(shape[0]))
        width = max(1, int(shape[1]))
        samples = max(8, int(axis_samples))
        y_step = max(1, math.ceil(height / samples))
        x_step = max(1, math.ceil(width / samples))
        try:
            lattice = frame[::y_step, ::x_step]
            digest.update(_bounded_bytes(lattice.tobytes(), 32_768))

            patch_radius = 3
            for y, x in (
                (0, 0),
                (0, width - 1),
                (height - 1, 0),
                (height - 1, width - 1),
                (height // 2, width // 2),
            ):
                y0 = max(0, y - patch_radius)
                y1 = min(height, y + patch_radius + 1)
                x0 = max(0, x - patch_radius)
                x1 = min(width, x + patch_radius + 1)
                patch = frame[y0:y1, x0:x1]
                digest.update(_bounded_bytes(patch.tobytes(), 4_096))
            return digest.digest()
        except (AttributeError, IndexError, TypeError, ValueError):
            # Tests and alternate capture adapters may use non-array frames.
            # Fall back to a bounded representation without weakening the
            # production NumPy-frame path above.
            pass

    digest.update(_bounded_bytes(frame))
    return digest.digest()


def exact_frame_identity(frame):
    """Fingerprint every byte for clock matching, where collisions are unsafe."""
    digest = hashlib.blake2b(digest_size=32)
    shape = tuple(getattr(frame, "shape", ()) or ())
    dtype = str(getattr(frame, "dtype", ""))
    digest.update(repr((shape, dtype)).encode("ascii", errors="replace"))
    try:
        digest.update(memoryview(frame))
    except (TypeError, ValueError, BufferError):
        try:
            digest.update(frame.tobytes())
        except AttributeError:
            digest.update(_bounded_bytes(frame, limit=1_048_576))
    return digest.digest()


class LiveStreamReader:
    """Continuously read one renewable live source in a daemon thread.

    ``source_factory`` is invoked for every connection attempt.  For signed HLS
    sessions this is the important boundary: a reconnect always receives a new
    URL rather than retrying an expired one.
    """

    def __init__(
        self,
        source_factory,
        capture_factory,
        recovery,
        state_callback=None,
        wall_time=None,
        monotonic=None,
        frame_identity=None,
        media_clock_factory=None,
        capture_position_milliseconds=None,
        media_frame_identity=None,
        media_clock_retry_seconds=2.0,
        frame_identity_history_size=256,
        duplicate_frame_limit=90,
        connection_max_age_seconds=None,
    ):
        self.source_factory = source_factory
        self.capture_factory = capture_factory
        self.recovery = recovery
        self.state_callback = state_callback
        self.wall_time = wall_time or time.time
        self.monotonic = monotonic or time.monotonic
        self.frame_identity = frame_identity or bounded_frame_identity
        self.media_clock_factory = media_clock_factory
        self.capture_position_milliseconds = capture_position_milliseconds
        self.media_frame_identity = media_frame_identity or exact_frame_identity
        self.media_clock_retry_seconds = max(
            0.1, float(media_clock_retry_seconds)
        )
        self.frame_identity_history_size = max(
            1, int(frame_identity_history_size)
        )
        self.duplicate_frame_limit = max(1, int(duplicate_frame_limit))
        if connection_max_age_seconds is None:
            self.connection_max_age_seconds = None
        else:
            maximum_age = float(connection_max_age_seconds)
            if not math.isfinite(maximum_age) or maximum_age <= 0.0:
                raise ValueError("connection_max_age_seconds must be positive")
            self.connection_max_age_seconds = maximum_age

        self._condition = threading.Condition()
        self._stop_event = threading.Event()
        self._thread = None
        self._sequence = 0
        self._latest = None
        self._recent_frame_identities = deque()
        self._recent_frame_identity_set = set()
        self._consecutive_duplicate_frames = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout=1.0):
        self.request_stop()
        self.join(timeout)

    def request_stop(self):
        self._stop_event.set()
        with self._condition:
            self._condition.notify_all()

    def join(self, timeout=1.0):
        if self._thread:
            self._thread.join(timeout=max(0.0, float(timeout)))

    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def snapshot(self, after_sequence=0):
        """Return the newest unconsumed frame, or ``None`` when unchanged."""
        with self._condition:
            if self._latest is None or self._sequence <= int(after_sequence):
                return None
            frame, source_epoch, source_monotonic, media_clock = self._latest
            return {
                "sequence": self._sequence,
                "frame": frame,
                "source_epoch": source_epoch,
                "source_monotonic": source_monotonic,
                "media_clock": media_clock,
            }

    def wait_for_frame(self, after_sequence=0, timeout=None):
        """Wait for a new frame; primarily useful for focused runtime tests."""
        with self._condition:
            self._condition.wait_for(
                lambda: self._stop_event.is_set()
                or (self._latest is not None and self._sequence > int(after_sequence)),
                timeout=timeout,
            )
        return self.snapshot(after_sequence)

    def _notify(self, state, error=None, delay_seconds=0.0):
        if self.state_callback:
            self.state_callback(
                state=state,
                error=error,
                failures=self.recovery.failures,
                delay_seconds=float(delay_seconds),
            )

    def _remember_frame_identity(self, identity):
        if len(self._recent_frame_identities) >= self.frame_identity_history_size:
            expired = self._recent_frame_identities.popleft()
            self._recent_frame_identity_set.discard(expired)
        self._recent_frame_identities.append(identity)
        self._recent_frame_identity_set.add(identity)

    def _run(self):
        while not self._stop_event.is_set():
            now = self.monotonic()
            if not self.recovery.can_retry(now):
                delay = max(0.0, self.recovery.next_retry_monotonic - now)
                self._stop_event.wait(min(delay, 0.5))
                continue

            cap = None
            try:
                # Keep this signed URL only in the connection-local stack so an
                # exact clock can be retried/re-anchored. Never publish, log, or
                # retain it in reader state; renew it on every outer attempt.
                source = self.source_factory()
                media_clock = None
                next_media_clock_retry = 0.0
                last_capture_position = None
                cap = self.capture_factory(source)
                if cap is None or not cap.isOpened():
                    raise RuntimeError("capture open failed")
                if self.media_clock_factory is None:
                    source = None

                connected = False
                connection_started = self.monotonic()
                while not self._stop_event.is_set():
                    if (
                        connected
                        and self.connection_max_age_seconds is not None
                        and self.monotonic() - connection_started
                        >= self.connection_max_age_seconds
                    ):
                        raise _ProactiveRenewal()
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        raise RuntimeError("frame read failed")

                    identity = self.frame_identity(frame)
                    if identity in self._recent_frame_identity_set:
                        self._consecutive_duplicate_frames += 1
                        if (
                            self._consecutive_duplicate_frames
                            >= self.duplicate_frame_limit
                        ):
                            raise RuntimeError("repeated frame content")
                        continue

                    source_epoch = self.wall_time()
                    source_monotonic = self.monotonic()
                    frame_media_clock = None
                    capture_position = None
                    if self.capture_position_milliseconds is not None:
                        try:
                            capture_position = self.capture_position_milliseconds(cap)
                        except (AttributeError, TypeError, ValueError):
                            capture_position = None
                    if (
                        media_clock is not None
                        and capture_position is not None
                        and last_capture_position is not None
                        and capture_position < last_capture_position - 0.5
                    ):
                        # A discontinuity/PTS reset invalidates the prior UTC
                        # mapping. Re-match this exact frame before trusting it.
                        media_clock = None
                        next_media_clock_retry = 0.0
                    if capture_position is not None:
                        last_capture_position = capture_position
                    if (
                        media_clock is None
                        and self.media_clock_factory is not None
                        and capture_position is not None
                        and source_monotonic >= next_media_clock_retry
                    ):
                        try:
                            media_clock = self.media_clock_factory(
                                source,
                                frame,
                                capture_position,
                                self.media_frame_identity,
                            )
                        except Exception:
                            # Clock metadata is additive. A temporary playlist,
                            # fragment, or match failure must not hide a
                            # decodable safety feed.
                            media_clock = None
                        if media_clock is None:
                            next_media_clock_retry = (
                                self.monotonic() + self.media_clock_retry_seconds
                            )
                    if (
                        media_clock is not None
                        and capture_position is not None
                    ):
                        try:
                            frame_media_clock = media_clock.metadata_at(
                                capture_position
                            )
                        except (AttributeError, TypeError, ValueError):
                            frame_media_clock = None
                    self._remember_frame_identity(identity)
                    self._consecutive_duplicate_frames = 0
                    self.recovery.record_success()
                    if not connected:
                        connected = True
                        self._notify("connected")

                    with self._condition:
                        self._sequence += 1
                        self._latest = (
                            frame,
                            source_epoch,
                            source_monotonic,
                            frame_media_clock,
                        )
                        self._condition.notify_all()
            except _ProactiveRenewal:
                # Keep the broadcaster in its current streaming state and the
                # latest trusted frame available while a fresh signed session
                # opens. Normal freshness/clock gates still fail closed if the
                # rotation takes too long.
                continue
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                # Capture/source factories should raise sanitized messages.  Do
                # not include the source URL in this status or in service logs.
                error = sanitize_source_error(exc)
                delay = self.recovery.record_failure(error, self.monotonic())
                self._notify("reconnecting", error=error, delay_seconds=delay)
                self._stop_event.wait(delay)
            finally:
                if cap is not None:
                    cap.release()
