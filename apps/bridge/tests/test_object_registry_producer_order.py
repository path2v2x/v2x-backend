"""Producer-time ordering regressions for the metadata registry."""

import pytest

from digital_twin_bridge.object_registry import ObjectRegistry


def detection(timestamp, confidence):
    return {
        "object_id": "stationary-1",
        "object_type": "car",
        "gps_location": {"latitude": 37.9, "longitude": -122.3},
        "confidence_score": confidence,
        "timestamp_utc": timestamp,
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_batch_keeps_newest_valid_producer_record_independent_of_order(reverse):
    older = detection("2026-07-09T22:00:00Z", 0.1)
    newer = detection("2026-07-09T22:00:10Z", 0.9)
    records = [older, newer]
    if reverse:
        records.reverse()

    registry = ObjectRegistry()
    registry.update_from_v2x(records)

    [tracked] = registry.get_all()
    assert tracked.timestamp_utc == newer["timestamp_utc"]
    assert tracked.confidence == pytest.approx(0.9)


def test_later_poll_with_older_or_invalid_record_cannot_regress_state():
    registry = ObjectRegistry()
    registry.update_from_v2x([detection("2026-07-09T22:00:10Z", 0.9)])
    registry.update_from_v2x(
        [
            detection("2026-07-09T22:00:00Z", 0.1),
            detection("not-a-time", 0.0),
        ]
    )

    [tracked] = registry.get_all()
    assert tracked.timestamp_utc == "2026-07-09T22:00:10Z"
    assert tracked.confidence == pytest.approx(0.9)


def test_invalid_producer_record_does_not_create_registry_entry():
    registry = ObjectRegistry()
    registry.update_from_v2x([detection("not-a-time", 0.5)])
    assert registry.get_all() == []


def test_future_producer_record_cannot_poison_newest_order(monkeypatch):
    from digital_twin_bridge import object_registry as registry_module

    now = 2_000_000_000.0
    monkeypatch.setattr(registry_module.time, "time", lambda: now)
    registry = ObjectRegistry()
    registry.update_from_v2x(
        [detection("2033-05-18T03:33:26Z", 1.0)]  # now + 6 seconds
    )
    assert registry.get_all() == []
