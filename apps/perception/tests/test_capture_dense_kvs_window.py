from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import capture_dense_kvs_window as dense_capture  # noqa: E402
from capture_dense_kvs_window import (  # noqa: E402
    DenseCaptureError,
    capture,
    request_windows,
)


def jpeg(value):
    image = np.full((48, 64, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def source_report(
    tmp_path,
    schema="v2x-detection-event-frame-capture/v2",
    timestamps=("2026-07-11T03:32:20.000Z", "2026-07-11T03:32:21.000Z"),
):
    frames = []
    for index in range(2):
        raw = jpeg(40 + index)
        path = tmp_path / f"source-{index}.jpg"
        path.write_bytes(raw)
        frames.append((path, hashlib.sha256(raw).hexdigest()))
    report = {
        "schema": schema,
        "events": [
            {
                "event_id": f"event-{index}",
                "object_id": "car-1",
                "camera_id": "ch4",
                "selected_frame_timestamp_utc": timestamps[index],
                "frame": {"path": str(path), "sha256": digest},
            }
            for index, (path, digest) in enumerate(frames)
        ],
    }
    path = tmp_path / "source-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


class FakeArchived:
    def __init__(self):
        self.calls = []

    def get_images(self, **kwargs):
        self.calls.append(kwargs)
        assert "NextToken" not in kwargs
        start = kwargs["StartTimestamp"]
        images = []
        timestamp = start.replace(tzinfo=timezone.utc)
        step = timedelta(milliseconds=kwargs["SamplingInterval"])
        while timestamp <= kwargs["EndTimestamp"]:
            raw = jpeg(40 + int(timestamp.timestamp() * 5) % 150)
            images.append({
                "TimeStamp": timestamp,
                "ImageContent": (
                    base64.b64encode(raw).decode() if not images else raw
                ),
            })
            timestamp += step
        assert len(images) <= kwargs["MaxResults"]
        return {"Images": images}


class FakeKvs:
    def get_data_endpoint(self, **kwargs):
        assert kwargs["APIName"] == "GET_IMAGES"
        return {"DataEndpoint": "https://signed.invalid"}


class FakeSession:
    def __init__(self):
        self.archived = FakeArchived()

    def factory(self, **kwargs):
        assert kwargs == {"profile_name": "path", "region_name": "us-west-2"}
        return self

    def client(self, name, **kwargs):
        if name == "kinesisvideo":
            return FakeKvs()
        assert name == "kinesis-video-archived-media"
        assert kwargs == {"endpoint_url": "https://signed.invalid"}
        return self.archived


def test_capture_binds_dense_frames_without_endpoint(tmp_path):
    session = FakeSession()
    output = tmp_path / "dense"
    report_path = capture(
        source_report(tmp_path),
        "ch4",
        "car-1",
        output,
        "path",
        session_factory=session.factory,
    )
    report = json.loads(report_path.read_text())
    assert report["schema"] == "v2x-dense-kvs-window/v1"
    assert report["frame_count"] == 16
    assert report["target_count"] == 16
    assert report["missing_target_count"] == 0
    assert report["resolution"] == [64, 48]
    assert report["safety"]["signed_endpoints_persisted"] is False
    assert "signed.invalid" not in report_path.read_text()
    assert len(session.archived.calls) == 1
    assert session.archived.calls[0]["SamplingInterval"] == 200
    assert session.archived.calls[0]["MaxResults"] == 25
    assert report["request_strategy"]["whole_second_aligned"] is True
    assert report["request_strategy"]["continuation_tokens_accepted"] is False
    for frame in report["frames"]:
        raw = (output / frame["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == frame["sha256"]


def test_capture_accepts_retained_v1_report(tmp_path):
    output = tmp_path / "dense"
    report_path = capture(
        source_report(tmp_path, "v2x-detection-event-frame-capture/v1"),
        "ch4",
        "car-1",
        output,
        "path",
        session_factory=FakeSession().factory,
    )
    assert json.loads(report_path.read_text())["frame_count"] == 16


def test_capture_chunks_long_grid_without_overlap_or_tokens(tmp_path):
    session = FakeSession()
    report_path = capture(
        source_report(
            tmp_path,
            timestamps=(
                "2026-07-11T03:32:20.000Z",
                "2026-07-11T03:32:32.000Z",
            ),
        ),
        "ch4",
        "car-1",
        tmp_path / "dense",
        "path",
        session_factory=session.factory,
    )
    report = json.loads(report_path.read_text())
    assert report["response_page_count"] == 4
    assert len(session.archived.calls) == 4
    for call in session.archived.calls:
        assert "NextToken" not in call
        assert call["MaxResults"] == 25
        assert call["StartTimestamp"].microsecond == 0
        assert call["EndTimestamp"].microsecond == 0
        assert (call["EndTimestamp"] - call["StartTimestamp"]).total_seconds() <= 4.0
    for left, right in zip(session.archived.calls, session.archived.calls[1:]):
        assert right["StartTimestamp"] == left["EndTimestamp"]
    assert report["duplicate_timestamp_count"] == 3


def test_request_chunks_align_fractional_range_and_overlap_boundaries():
    start = datetime.fromisoformat("2026-07-11T03:32:20.125+00:00")
    windows = request_windows(
        start,
        datetime.fromisoformat("2026-07-11T03:32:25.050+00:00"),
        200,
    )
    assert len(windows) == 2
    assert all(left < right for left, right in windows)
    assert windows[0][0] == datetime.fromisoformat("2026-07-11T03:32:20+00:00")
    assert windows[0][1] == datetime.fromisoformat("2026-07-11T03:32:24+00:00")
    assert windows[1][0] == windows[0][1]
    assert windows[1][1] == datetime.fromisoformat("2026-07-11T03:32:26+00:00")


def test_request_chunks_reject_zero_range():
    timestamp = datetime.fromisoformat("2026-07-11T03:32:20+00:00")
    with pytest.raises(DenseCaptureError, match="positive time range"):
        request_windows(timestamp, timestamp, 200)


def test_request_windows_support_every_valid_sampling_interval():
    start = datetime.fromisoformat("2026-07-11T03:32:20+00:00")
    for sampling_ms in range(200, 20001):
        end = start + timedelta(milliseconds=sampling_ms * 2)
        _origin, targets = dense_capture.sampling_targets(
            start, end, sampling_ms
        )
        assert targets == [
            start,
            start + timedelta(milliseconds=sampling_ms),
            end,
        ]
        windows = request_windows(start, end, sampling_ms)
        assert 1 <= len(windows) <= 3
        assert all(
            left.microsecond == 0
            and right.microsecond == 0
            and (right - left).total_seconds() <= 4
            for left, right in windows
        )


def test_target_matcher_does_not_shift_grid_when_edge_candidate_is_missing():
    start = datetime.fromisoformat("2026-07-11T03:32:20+00:00")
    targets = [start + timedelta(milliseconds=value) for value in (0, 200, 400)]
    candidates = [
        (target, b"jpeg", object())
        for target in targets[1:]
    ]
    matched = dense_capture.match_sampling_targets(targets, candidates)
    assert [(target, timestamp) for target, timestamp, _raw, _image in matched] == [
        (targets[1], targets[1]),
        (targets[2], targets[2]),
    ]


@pytest.mark.parametrize(
    ("sampling_ms", "duration_seconds", "expected_count"),
    [(350, 10, 29), (999, 25, 26)],
)
def test_capture_preserves_one_grid_phase_across_chunk_boundaries(
    tmp_path, sampling_ms, duration_seconds, expected_count
):
    session = FakeSession()
    start = datetime.fromisoformat("2026-07-11T03:32:20+00:00")
    report_path = capture(
        source_report(
            tmp_path,
            timestamps=(
                start.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                (start + timedelta(seconds=duration_seconds))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            ),
        ),
        "ch4",
        "car-1",
        tmp_path / "dense",
        "path",
        padding_seconds=0,
        sampling_ms=sampling_ms,
        session_factory=session.factory,
    )
    report = json.loads(report_path.read_text())
    assert report["target_count"] == expected_count
    assert report["frame_count"] == expected_count
    assert report["missing_target_count"] == 0
    targets = [
        datetime.fromisoformat(row["target_timestamp_utc"].replace("Z", "+00:00"))
        for row in report["frames"]
    ]
    assert {
        round((right - left).total_seconds() * 1000)
        for left, right in zip(targets, targets[1:])
    } == {sampling_ms}
    assert all(
        abs(row["target_offset_ms"])
        <= report["request_strategy"]["maximum_target_offset_ms"]
        for row in report["frames"]
    )
    assert report["request_strategy"]["single_target_phase_across_requests"] is True
    assert len(session.archived.calls) > 1
    for call in session.archived.calls:
        assert call["SamplingInterval"] == 200
        assert call["StartTimestamp"].microsecond == 0
        assert call["EndTimestamp"].microsecond == 0
        assert (call["EndTimestamp"] - call["StartTimestamp"]).total_seconds() <= 4


@pytest.mark.parametrize("destination_kind", ["file", "directory"])
def test_capture_concurrent_destination_is_never_replaced(
    tmp_path, monkeypatch, destination_kind
):
    output = tmp_path / "dense"
    real_publish = dense_capture.publish_directory_no_replace
    competing = {}

    def create_competing_output_then_publish(source, destination):
        if destination_kind == "file":
            Path(destination).write_bytes(b"competing evidence")
        else:
            Path(destination).mkdir()
        competing["inode"] = Path(destination).lstat().st_ino
        real_publish(source, destination)

    monkeypatch.setattr(
        dense_capture,
        "publish_directory_no_replace",
        create_competing_output_then_publish,
    )
    with pytest.raises(DenseCaptureError, match="output already exists"):
        capture(
            source_report(tmp_path),
            "ch4",
            "car-1",
            output,
            "path",
            session_factory=FakeSession().factory,
        )
    assert output.lstat().st_ino == competing["inode"]
    if destination_kind == "file":
        assert output.read_bytes() == b"competing evidence"
    else:
        assert list(output.iterdir()) == []
    assert list(tmp_path.glob(".dense.tmp-*")) == []


def test_capture_cleans_staging_when_atomic_publish_fails(tmp_path, monkeypatch):
    def fail_publish(_source, _destination):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(
        dense_capture, "publish_directory_no_replace", fail_publish
    )
    with pytest.raises(DenseCaptureError, match="publication failed"):
        capture(
            source_report(tmp_path),
            "ch4",
            "car-1",
            tmp_path / "dense",
            "path",
            session_factory=FakeSession().factory,
        )
    assert not (tmp_path / "dense").exists()
    assert list(tmp_path.glob(".dense.tmp-*")) == []


def test_capture_cleans_staging_when_frame_write_fails(tmp_path, monkeypatch):
    source = source_report(tmp_path)
    real_write_bytes = Path.write_bytes

    def fail_staged_frame_write(path, value):
        if ".dense.tmp-" in str(path):
            raise OSError("synthetic staging failure")
        return real_write_bytes(path, value)

    monkeypatch.setattr(Path, "write_bytes", fail_staged_frame_write)
    with pytest.raises(DenseCaptureError, match="staging failed"):
        capture(
            source,
            "ch4",
            "car-1",
            tmp_path / "dense",
            "path",
            session_factory=FakeSession().factory,
        )
    assert not (tmp_path / "dense").exists()
    assert list(tmp_path.glob(".dense.tmp-*")) == []


def test_capture_rejects_unexpected_bounded_page_token(tmp_path):
    class TokenArchived(FakeArchived):
        def get_images(self, **kwargs):
            response = super().get_images(**kwargs)
            response["NextToken"] = "opaque"
            return response

    session = FakeSession()
    session.archived = TokenArchived()
    with pytest.raises(DenseCaptureError, match="unexpectedly requires pagination"):
        capture(
            source_report(tmp_path),
            "ch4",
            "car-1",
            tmp_path / "dense",
            "path",
            session_factory=session.factory,
        )


def test_capture_rejects_tampered_source_frame(tmp_path):
    report_path = source_report(tmp_path)
    report = json.loads(report_path.read_text())
    Path(report["events"][0]["frame"]["path"]).write_bytes(b"tampered")
    with pytest.raises(DenseCaptureError, match="hash does not match"):
        capture(
            report_path,
            "ch4",
            "car-1",
            tmp_path / "dense",
            "path",
            session_factory=FakeSession().factory,
        )


def test_capture_rejects_window_over_api_bound(tmp_path):
    with pytest.raises(DenseCaptureError, match="100-image"):
        capture(
            source_report(tmp_path),
            "ch4",
            "car-1",
            tmp_path / "dense",
            "path",
            padding_seconds=10.0,
            sampling_ms=200,
            session_factory=FakeSession().factory,
        )
