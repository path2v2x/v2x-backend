"""Adversarial contract and strict reviewed-placement tests."""

import hashlib
import json

import pytest

from digital_twin_bridge import twin_sync as twin_sync_module
from digital_twin_bridge.reviewed_localization import (
    CameraPlacementContext,
    ReviewedLocalizationError,
    ReviewedPlacementContext,
    build_runtime_context,
    seal_contract,
    validate_contract,
)
from digital_twin_bridge.twin_sync import TwinSync
from tests.conftest import MockBlueprint


HASHES = {letter: letter * 64 for letter in "abcdef1234567890"}


def context():
    return ReviewedPlacementContext(
        map_name="TestMap",
        opendrive_sha256=HASHES["a"],
        cameras_json_sha256=HASHES["b"],
        cameras={
            "ch1": CameraPlacementContext(HASHES["c"], HASHES["d"]),
            "ch2": CameraPlacementContext(HASHES["1"], HASHES["2"]),
        },
    )


def detection(camera_id="ch1", sample_index=0, seconds=0, position=None):
    position = position or {"x": 10.0, "y": 20.0, "z": 1.25}
    timestamp = f"2026-07-13T20:00:{seconds:02d}.000Z"
    camera = context().cameras[camera_id]
    value = {
        "event_id": f"event-{sample_index}",
        "object_id": "global_car_reviewed",
        "object_type": "car",
        "timestamp_utc": timestamp,
        "media_timestamp_utc": timestamp,
        "timestamp_schema_version": 2,
        "media_time_trusted": True,
        "media_clock": {
            "schema_version": 1,
            "source": "hls_ext_x_program_date_time",
            "matching_method": "exact_same_session_pts",
            "session_id": f"session-{camera_id}",
            "position_milliseconds": float(seconds * 1000),
        },
        "device_id": f"cam-001-{camera_id}",
        "track_id": 42,
        "bbox": {"x1": 100.0, "y1": 100.0, "x2": 300.0, "y2": 500.0},
        # Deliberately spoofed baseline. Strict mode must never consume it.
        "gps_location": {"latitude": 80.0, "longitude": 80.0},
        "camera_data": {"bifocal_metadata": {"frame": 5 + sample_index}},
        "raw_observation": {
            "native_resolution": [1280, 960],
            "fingerprints": {
                "cameras_json_sha256": HASHES["b"],
                "camera_config_sha256": camera.camera_config_sha256,
                "detector_model_sha256": HASHES["e"],
                "detector_config_sha256": HASHES["f"],
            },
            "optimizer_contract": {
                "gps_location_is_derived_baseline": True,
                "acceptance_eligible": False,
            },
        },
    }
    value["reviewed_localization"] = seal_contract({
        "schema": "v2x-reviewed-vehicle-localization/v1",
        "event_id": value["event_id"],
        "camera_id": camera_id,
        "global_track_id": value["object_id"],
        "trajectory_id": "trajectory-reviewed-1",
        "sample_index": sample_index,
        "source": {
            "frame": {
                "sha256": HASHES["3"],
                "mask_sha256": HASHES["4"],
                "native_resolution": [1280, 960],
                "frame_number": 5 + sample_index,
            },
            "detector": {
                "model_sha256": HASHES["e"],
                "config_sha256": HASHES["f"],
            },
            "camera": {
                "cameras_json_sha256": HASHES["b"],
                "camera_config_sha256": camera.camera_config_sha256,
                "intrinsics_artifact_sha256": camera.intrinsics_artifact_sha256,
            },
            "map": {"name": "TestMap", "opendrive_sha256": HASHES["a"]},
        },
        "contact": {
            "method": "reviewed_vehicle_footprint_midpoint",
            "left_ground_pixel": [500.0, 700.0],
            "right_ground_pixel": [700.0, 700.0],
            "footprint_midpoint_pixel": [600.0, 700.0],
            "covariance_px2": [[4.0, 1.0], [1.0, 9.0]],
        },
        "timing": {
            "method": "exact_same_session_pts",
            "trusted": True,
            "session_id": f"session-{camera_id}",
            "pts_seconds": float(seconds),
            "media_timestamp_utc": timestamp,
            "timestamp_error_ms": 0.25,
        },
        "review": {
            "decision": "accepted",
            "reviewer": {"kind": "human", "id": "reviewer-a"},
            "consensus": {
                "method": "independent_review_consensus",
                "artifact_sha256": HASHES["5"],
                "reviewer_ids": ["reviewer-a", "reviewer-b"],
            },
            "factor_graph": {
                "artifact_sha256": HASHES["6"],
                "acceptance_eligible": True,
            },
        },
        "identity": {
            "status": "unambiguous",
            "global_track_id": value["object_id"],
            "trajectory_id": "trajectory-reviewed-1",
            "association_method": "reviewed_multicamera_trajectory",
            "evidence_sha256": HASHES["7"],
            "camera_ids": ["ch1", "ch2"],
        },
        "placement": {
            "coordinate_frame": "carla_world",
            "position_semantics": "ue5_actor_center",
            "position_m": position,
            "covariance_m2": [
                [0.25, 0.0, 0.0],
                [0.0, 0.36, 0.0],
                [0.0, 0.0, 0.04],
            ],
            "uncertainty_m": 0.75,
            "heading_deg": 37.0,
            "dimensions_m": {"length": 4.7, "width": 1.9, "height": 1.6},
            "blueprint_family": "passenger_car",
        },
    })
    return value


def reseal(value):
    value["reviewed_localization"] = seal_contract(value["reviewed_localization"])
    return value


def strict_sync(mock_world):
    pools = {
        "vehicle.*": [
            MockBlueprint("vehicle.tesla.model3"),
            MockBlueprint("vehicle.audi.tt"),
            MockBlueprint("vehicle.ford.truck"),
        ],
        "walker.pedestrian.*": [],
    }
    mock_world._blueprint_library.filter = lambda pattern: list(pools.get(pattern, []))
    return TwinSync(
        mock_world,
        mock_world.get_map(),
        reviewed_placement="strict",
        reviewed_context=context(),
    )


def test_valid_contract_uses_reviewed_footprint_midpoint_not_bbox_center():
    value = detection()
    reviewed = validate_contract(
        value["reviewed_localization"], value, context()
    )
    bbox = value["bbox"]
    bbox_center = [(bbox["x1"] + bbox["x2"]) / 2, (bbox["y1"] + bbox["y2"]) / 2]
    assert reviewed["footprint_midpoint_pixel"] == [600.0, 700.0]
    assert reviewed["footprint_midpoint_pixel"] != bbox_center
    assert reviewed["position_m"] == {"x": 10.0, "y": 20.0, "z": 1.25}


def test_runtime_context_verifies_intrinsics_file_and_opendrive(tmp_path, mock_world):
    intrinsics = tmp_path / "intrinsics.json"
    intrinsics.write_text('{"camera":"ch1"}\n')
    cameras = tmp_path / "cameras.json"
    camera = {
        "id": "ch1",
        "intrinsics_calibration": {
            "artifact_path": str(intrinsics),
            "artifact_sha256": hashlib.sha256(intrinsics.read_bytes()).hexdigest(),
        },
    }
    cameras.write_text(json.dumps({"cameras": [camera]}) + "\n")
    mock_world.get_map().to_opendrive = lambda: "<OpenDRIVE/>"

    runtime = build_runtime_context(mock_world.get_map(), str(cameras))

    assert runtime.map_name == "TestMap"
    assert runtime.opendrive_sha256 == hashlib.sha256(b"<OpenDRIVE/>").hexdigest()
    assert runtime.cameras_json_sha256 == hashlib.sha256(cameras.read_bytes()).hexdigest()

    intrinsics.write_text("tampered\n")
    with pytest.raises(ReviewedLocalizationError, match="artifact_mismatch"):
        build_runtime_context(mock_world.get_map(), str(cameras))


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda c: c["identity"].update(status="ambiguous"), "identity_ambiguous"),
        (lambda c: c["source"]["map"].update(opendrive_sha256=HASHES["9"]), "active_opendrive_mismatch"),
        (lambda c: c["source"]["camera"].update(camera_config_sha256=HASHES["9"]), "active_camera_config_mismatch"),
        (lambda c: c["placement"].update(uncertainty_m=2.01), "placement_uncertainty_exceeds_2m"),
        (lambda c: c["placement"].update(covariance_m2=[[1.0, 2.0, 0.0], [2.0, 1.0, 0.0], [0.0, 0.0, 1.0]]), "placement_covariance_not_psd"),
        (lambda c: c["contact"].update(footprint_midpoint_pixel=[601.0, 700.0]), "footprint_midpoint_mismatch"),
        (lambda c: c["timing"].update(timestamp_error_ms=1.01), "timing_error_exceeds_exact_gate"),
        (lambda c: c["review"]["factor_graph"].update(acceptance_eligible=False), "factor_graph_not_acceptance_eligible"),
        (lambda c: c["source"]["frame"].update(sha256=None), "native_frame_hash_invalid"),
        (lambda c: c.update(unsigned_semantics="spoof"), "contract_fields_invalid"),
    ],
)
def test_adversarial_contracts_fail_closed(mutation, reason):
    value = detection()
    mutation(value["reviewed_localization"])
    reseal(value)
    with pytest.raises(ReviewedLocalizationError, match=reason):
        validate_contract(value["reviewed_localization"], value, context())


def test_tamper_without_resealing_is_rejected():
    value = detection()
    value["reviewed_localization"]["placement"]["position_m"]["x"] = 999.0
    with pytest.raises(ReviewedLocalizationError, match="contract_hash_mismatch"):
        validate_contract(value["reviewed_localization"], value, context())


def test_nonfinite_covariance_is_rejected_before_consumption():
    value = detection()
    value["reviewed_localization"]["placement"]["covariance_m2"][0][0] = float("nan")
    with pytest.raises(ReviewedLocalizationError, match="contract_not_canonical_json"):
        validate_contract(value["reviewed_localization"], value, context())


def test_timestamp_and_session_spoof_are_rejected():
    value = detection()
    value["media_clock"]["session_id"] = "other-session"
    with pytest.raises(ReviewedLocalizationError, match="timing_media_clock_mismatch"):
        validate_contract(value["reviewed_localization"], value, context())


def test_strict_mode_uses_exact_world_actor_center_without_lane_or_gps(
    mock_world, monkeypatch
):
    sync = strict_sync(mock_world)
    monkeypatch.setattr(
        twin_sync_module,
        "gps_to_carla",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("GPS used")),
    )
    mock_world.get_map().get_waypoint = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(AssertionError("lane snap used"))
    )

    sync._apply([detection()])

    actor = mock_world.get_actor(next(iter(sync.actor_ids())))
    transform = actor.get_transform()
    assert (transform.location.x, transform.location.y, transform.location.z) == (
        10.0,
        20.0,
        1.25,
    )
    assert transform.rotation.yaw == 37.0
    status = sync.status()["objects"][0]
    assert status["reviewed_to_actor_planar_m"] == pytest.approx(0.0)
    assert status["placement_metric_status"] == "reviewed_world_exact"
    assert status["placement_provenance"]["uncertainty_m"] == 0.75


def test_strict_mode_has_no_baseline_fallback_and_invalid_update_does_not_move(
    mock_world,
):
    sync = strict_sync(mock_world)
    missing = detection()
    missing.pop("reviewed_localization")
    sync._apply([missing])
    assert sync.actor_ids() == set()
    assert sync.status()["strict_rejections"] == {"reviewed_localization_missing": 1}

    first = detection()
    sync._apply([first])
    actor = mock_world.get_actor(next(iter(sync.actor_ids())))
    invalid = detection(sample_index=1, seconds=1, position={"x": 50.0, "y": 60.0, "z": 1.0})
    invalid["reviewed_localization"]["source"]["map"]["name"] = "SpoofMap"
    reseal(invalid)
    sync._apply([invalid])
    assert actor.get_transform().location.x == 10.0
    assert sync.status()["strict_rejections"]["active_map_name_mismatch"] == 1


def test_multicamera_trajectory_keeps_one_actor_and_exact_latest_sample(mock_world):
    sync = strict_sync(mock_world)
    first = detection(camera_id="ch1", sample_index=0, seconds=0)
    second = detection(
        camera_id="ch2",
        sample_index=1,
        seconds=1,
        position={"x": 11.5, "y": 20.25, "z": 1.25},
    )
    sync._apply([first])
    actor_id = next(iter(sync.actor_ids()))
    sync._apply([second])
    assert sync.actor_ids() == {actor_id}
    actor = mock_world.get_actor(actor_id)
    assert actor.get_transform().location.x == 11.5
    status = sync.status()["objects"][0]
    assert status["trajectory_sample_index"] == 1
    assert status["reviewed_contract_sha256"] == second["reviewed_localization"]["contract_sha256"]


def test_nonmonotonic_trajectory_is_rejected_without_moving_actor(mock_world):
    sync = strict_sync(mock_world)
    sync._apply([detection(sample_index=1, seconds=1)])
    actor = mock_world.get_actor(next(iter(sync.actor_ids())))
    stale = detection(sample_index=0, seconds=0, position={"x": 99.0, "y": 99.0, "z": 1.0})
    sync._apply([stale])
    assert actor.get_transform().location.x == 10.0
    assert sync.status()["strict_rejections"]["trajectory_sample_not_monotonic"] == 1


def test_failed_exact_actor_transform_keeps_last_accepted_provenance(mock_world):
    sync = strict_sync(mock_world)
    first = detection()
    sync._apply([first])
    actor = mock_world.get_actor(next(iter(sync.actor_ids())))

    def fail_transform(_transform):
        raise RuntimeError("injected transform failure")

    actor.set_transform = fail_transform
    second = detection(
        camera_id="ch2",
        sample_index=1,
        seconds=1,
        position={"x": 50.0, "y": 60.0, "z": 1.25},
    )
    sync._apply([second])
    status = sync.status()["objects"][0]
    assert status["event_id"] == first["event_id"]
    assert status["reviewed_contract_sha256"] == first["reviewed_localization"]["contract_sha256"]
    assert status["reviewed_world_location"] == {"x": 10.0, "y": 20.0, "z": 1.25}
    assert sync.status()["strict_rejections"]["strict_exact_transform_failed"] == 1


def test_blueprint_digest_is_stable_and_cleanup_is_unchanged(mock_world):
    first_sync = strict_sync(mock_world)
    first_sync._apply([detection()])
    first_status = first_sync.status()["objects"][0]
    first_actor = mock_world.get_actor(first_status["actor_id"])
    first_type = first_actor.type_id
    first_digest = first_status["blueprint_selection_digest"]
    first_sync.stop()
    assert first_actor.is_destroyed

    second_sync = strict_sync(mock_world)
    second_sync._apply([detection()])
    second_status = second_sync.status()["objects"][0]
    assert second_status["blueprint_selection_digest"] == first_digest
    assert mock_world.get_actor(second_status["actor_id"]).type_id == first_type
