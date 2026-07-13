"""Adversarial contract and strict reviewed-placement tests."""

import hashlib
import json
import math
from datetime import datetime, timezone

import pytest

from digital_twin_bridge import twin_sync as twin_sync_module
from digital_twin_bridge.reviewed_localization import (
    CameraPlacementContext,
    ReviewedLocalizationError,
    ReviewedPlacementContext,
    build_runtime_context,
    canonical_json_bytes,
    contract_sha256,
    placement_key_sha256,
    seal_contract,
    validate_contract,
)
from digital_twin_bridge.twin_sync import TwinSync
from tests.conftest import MockBlueprint


HASHES = {letter: letter * 64 for letter in "abcdef1234567890"}
AUTHORITY_KEY = b"review-authority-test-key-material"[:32]
AUTHORITY_KEY_ID = "reviewer-a"


def blueprint_binding(global_track_id="global_car_reviewed"):
    families = {
        "passenger_car": ["vehicle.audi.tt", "vehicle.tesla.model3"],
        "truck": ["vehicle.ford.truck"],
        "bus": ["vehicle.ford.truck"],
    }
    pool = families["passenger_car"]
    key = placement_key_sha256(global_track_id, "passenger_car")
    selected = pool[int.from_bytes(bytes.fromhex(key)[:8], "big") % len(pool)]
    return {
        "catalog_sha256": hashlib.sha256(canonical_json_bytes(families)).hexdigest(),
        "pool_sha256": hashlib.sha256(canonical_json_bytes(pool)).hexdigest(),
        "selected_blueprint_id": selected,
        "expected_dimensions_m": {"length": 4.7, "width": 1.9, "height": 1.6},
        "dimension_tolerance_m": 0.25,
    }


def context():
    return ReviewedPlacementContext(
        map_name="TestMap",
        opendrive_sha256=HASHES["a"],
        cameras_json_sha256=HASHES["b"],
        cameras={
            "ch1": CameraPlacementContext(HASHES["c"], HASHES["d"], HASHES["8"]),
            "ch2": CameraPlacementContext(HASHES["1"], HASHES["2"], HASHES["9"]),
        },
        static_calibration_sha256=HASHES["0"],
        authority_keys={AUTHORITY_KEY_ID: AUTHORITY_KEY},
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
        "bbox": {"x1": 450.0, "y1": 350.0, "x2": 750.0, "y2": 720.0},
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
                "source_kind": "persisted_native_frame_and_instance_mask",
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
                "intrinsics_report_sha256": camera.intrinsics_report_sha256,
                "static_calibration_sha256": HASHES["0"],
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
            "independent_reference": {
                "artifact_sha256": HASHES["8"],
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
            "transition": (
                None
                if sample_index == 0
                else {
                    "previous_event_id": f"event-{sample_index - 1}",
                    "accepted": True,
                    "ambiguity": False,
                    "appearance_similarity": 0.88,
                    "transit_seconds": float(seconds),
                    "distance_m": math.sqrt(
                        (position["x"] - 10.0) ** 2
                        + (position["y"] - 20.0) ** 2
                        + (position["z"] - 1.25) ** 2
                    ),
                    "speed_mps": math.sqrt(
                        (position["x"] - 10.0) ** 2
                        + (position["y"] - 20.0) ** 2
                        + (position["z"] - 1.25) ** 2
                    ) / float(seconds),
                    "acceleration_mps2": math.sqrt(
                        (position["x"] - 10.0) ** 2
                        + (position["y"] - 20.0) ** 2
                        + (position["z"] - 1.25) ** 2
                    ) / (float(seconds) ** 2),
                    "trajectory_covariance_m2": [
                        [0.3, 0.0, 0.0],
                        [0.0, 0.3, 0.0],
                        [0.0, 0.0, 0.1],
                    ],
                    "pair_evidence_sha256": HASHES["9"],
                }
            ),
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
            "independent_reference": {
                "position_m": {
                    "x": position["x"] + 0.1,
                    "y": position["y"],
                    "z": position["z"],
                },
                "error_m": 0.1,
            },
            "blueprint": blueprint_binding(),
        },
    }, AUTHORITY_KEY_ID, AUTHORITY_KEY)
    return value


def reseal(value):
    value["reviewed_localization"] = seal_contract(
        value["reviewed_localization"], AUTHORITY_KEY_ID, AUTHORITY_KEY
    )
    return value


def strict_sync(mock_world, detection_max_age=1e12):
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
        detection_max_age=detection_max_age,
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
    hashes = [f"{index:064x}" for index in range(1, 13)]
    matrix = [[1000.0, 0, 640.0], [0, 1000.0, 480.0], [0, 0, 1]]
    distortion = {key: 0.0 for key in ("k1", "k2", "p1", "p2", "k3")}
    normalized = {
        "method": "checkerboard",
        "image_count": 12,
        "source_images_sha256": hashes,
        "rms_reprojection_error_px": 0.5,
        "resolution": [1280, 960],
        "camera_matrix": matrix,
        "distortion": distortion,
    }
    intrinsics = tmp_path / "intrinsics.json"
    intrinsics.write_text(json.dumps(normalized) + "\n")
    report = tmp_path / "intrinsics-report.json"
    report.write_text(json.dumps({
        "schema": "v2x-checkerboard-calibration-report/v1",
        "accepted": [{"sha256": value} for value in hashes[:10]],
        "holdouts": [{"sha256": value} for value in hashes[10:]],
        "holdout_metrics": {"rmse_px": 0.6, "max_error_px": 1.2},
    }) + "\n")
    cameras = tmp_path / "cameras.json"
    camera = {
        "id": "ch1",
        "intrinsics": {
            "width": 1280, "height": 960,
            "fx": 1000.0, "fy": 1000.0, "cx": 640.0, "cy": 480.0,
        },
        "intrinsics_calibration": {
            **normalized,
            "artifact_path": str(intrinsics),
            "artifact_sha256": hashlib.sha256(intrinsics.read_bytes()).hexdigest(),
            "report_path": str(report),
            "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        },
    }
    cameras.write_text(json.dumps({"cameras": [camera]}) + "\n")
    mock_world.get_map().to_opendrive = lambda: "<OpenDRIVE/>"
    opendrive_hash = hashlib.sha256(b"<OpenDRIVE/>").hexdigest()
    camera_hash = hashlib.sha256(
        json.dumps(camera, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    static = tmp_path / "static.json"
    static.write_text(json.dumps({
        "schema": "v2x-static-camera-survey-manifest/v1",
        "source_cameras_json_sha256": hashlib.sha256(cameras.read_bytes()).hexdigest(),
        "map": {"name": "TestMap", "opendrive_sha256": opendrive_hash},
        "heldout_gate": {
            "passed": True,
            "reference_resolution": [1280, 960],
            "cameras": {
                "ch1": {
                    "camera_config_sha256": camera_hash,
                    "landmark_count": 4,
                    "landmark_rmse_px": 5.0,
                    "landmark_p95_px": 8.0,
                    "landmark_max_px": 10.0,
                    "road_rmse_px": 4.0,
                    "road_max_px": 8.0,
                }
            },
        },
    }) + "\n")
    authority = tmp_path / "authority.json"
    authority.write_text(json.dumps({
        "schema": "v2x-review-authority-keys/v1",
        "keys": {AUTHORITY_KEY_ID: AUTHORITY_KEY.hex()},
    }) + "\n")

    runtime = build_runtime_context(
        mock_world.get_map(), str(cameras), str(static), str(authority)
    )

    assert runtime.map_name == "TestMap"
    assert runtime.opendrive_sha256 == opendrive_hash
    assert runtime.cameras_json_sha256 == hashlib.sha256(cameras.read_bytes()).hexdigest()

    assert runtime.static_calibration_sha256 == hashlib.sha256(static.read_bytes()).hexdigest()
    assert runtime.authority_keys[AUTHORITY_KEY_ID] == AUTHORITY_KEY

    intrinsics.write_text("tampered\n")
    with pytest.raises(ReviewedLocalizationError, match="artifact_(unavailable|mismatch)"):
        build_runtime_context(
            mock_world.get_map(), str(cameras), str(static), str(authority)
        )


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


def test_self_hash_recomputation_cannot_forge_review_authority():
    value = detection()
    contract = value["reviewed_localization"]
    contract["placement"]["position_m"]["x"] = 12.0
    contract["placement"]["independent_reference"]["position_m"]["x"] = 12.1
    contract["contract_sha256"] = contract_sha256(contract)
    with pytest.raises(ReviewedLocalizationError, match="authority_signature_mismatch"):
        validate_contract(contract, value, context())


@pytest.mark.parametrize(
    ("field", "covariance", "reason"),
    [
        (
            "contact",
            [[1000.0, 900.0], [900.0, 1000.0]],
            "contact_covariance_exceeds_gate",
        ),
        (
            "placement",
            [[3.0, 3.0, 0.0], [3.0, 3.0, 0.0], [0.0, 0.0, 0.1]],
            "placement_uncertainty_understates_covariance",
        ),
    ],
)
def test_correlated_covariance_uses_largest_eigenvalue(field, covariance, reason):
    value = detection()
    if field == "contact":
        value["reviewed_localization"]["contact"]["covariance_px2"] = covariance
    else:
        value["reviewed_localization"]["placement"]["covariance_m2"] = covariance
        value["reviewed_localization"]["placement"]["uncertainty_m"] = 2.0
    reseal(value)
    with pytest.raises(ReviewedLocalizationError, match=reason):
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
    assert status["placement_metric_status"] == "independent_reference"
    assert status["independent_reference_to_actor_m"] == pytest.approx(0.1)
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


def test_trajectory_must_start_at_zero(mock_world):
    sync = strict_sync(mock_world)
    sync._apply([detection(sample_index=1, seconds=1)])
    assert sync.actor_ids() == set()
    assert sync.status()["strict_rejections"]["trajectory_must_start_at_zero"] == 1


def test_same_time_large_teleport_is_rejected(mock_world):
    sync = strict_sync(mock_world)
    sync._apply([detection()])
    actor = mock_world.get_actor(next(iter(sync.actor_ids())))
    second = detection(
        camera_id="ch2",
        sample_index=1,
        seconds=1,
        position={"x": 14_010.0, "y": 20.0, "z": 1.25},
    )
    contract = second["reviewed_localization"]
    contract["placement"]["independent_reference"]["position_m"] = {
        "x": 14_010.1, "y": 20.0, "z": 1.25,
    }
    contract["identity"]["transition"].update({
        "distance_m": 1.0,
        "speed_mps": 1.0,
        "acceleration_mps2": 1.0,
    })
    second["timestamp_utc"] = detection()["timestamp_utc"]
    second["media_timestamp_utc"] = second["timestamp_utc"]
    second["media_clock"]["position_milliseconds"] = 0.0
    contract["timing"].update({
        "pts_seconds": 0.0,
        "media_timestamp_utc": second["timestamp_utc"],
    })
    reseal(second)
    sync._apply([second])
    assert actor.get_transform().location.x == 10.0
    assert sync.status()["strict_rejections"][
        "trajectory_timestamp_not_strictly_increasing"
    ] == 1


def test_nonmonotonic_trajectory_is_rejected_without_moving_actor(mock_world):
    sync = strict_sync(mock_world)
    sync._apply([detection(sample_index=0, seconds=0)])
    actor = mock_world.get_actor(next(iter(sync.actor_ids())))
    stale = detection(sample_index=0, seconds=0, position={"x": 99.0, "y": 99.0, "z": 1.0})
    sync._apply([stale])
    assert actor.get_transform().location.x == 10.0
    assert sync.status()["strict_rejections"]["trajectory_sample_not_contiguous"] == 1


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
        position={"x": 11.5, "y": 20.25, "z": 1.25},
    )
    sync._apply([second])
    status = sync.status()["objects"][0]
    assert status["event_id"] == first["event_id"]
    assert status["reviewed_contract_sha256"] == first["reviewed_localization"]["contract_sha256"]
    assert status["reviewed_world_location"] == {"x": 10.0, "y": 20.0, "z": 1.25}
    assert sync.status()["strict_rejections"]["strict_transform_rollback_failed"] == 1


def test_exact_transform_failure_rolls_back_before_metadata_commit(mock_world):
    sync = strict_sync(mock_world)
    first = detection()
    sync._apply([first])
    actor = mock_world.get_actor(next(iter(sync.actor_ids())))
    original_set_transform = actor.set_transform

    def fail_only_new_transform(transform):
        if abs(transform.location.x - 11.5) < 1e-9:
            raise RuntimeError("injected intended-transform failure")
        original_set_transform(transform)

    actor.set_transform = fail_only_new_transform
    second = detection(
        camera_id="ch2",
        sample_index=1,
        seconds=1,
        position={"x": 11.5, "y": 20.25, "z": 1.25},
    )
    sync._apply([second])
    status = sync.status()["objects"][0]
    assert actor.get_transform().location.x == 10.0
    assert status["event_id"] == first["event_id"]
    assert status["trajectory_sample_index"] == 0
    assert sync.status()["strict_rejections"]["strict_exact_transform_failed"] == 1


def test_live_freshness_gate_is_not_applied_to_replay(mock_world):
    value = detection()
    media_epoch = datetime.fromisoformat(
        value["media_timestamp_utc"].replace("Z", "+00:00")
    ).astimezone(timezone.utc).timestamp()

    stale = strict_sync(mock_world, detection_max_age=8.0)
    stale._apply([value], now=media_epoch + 9.0)
    assert stale.actor_ids() == set()
    assert stale.status()["strict_rejections"]["strict_live_detection_stale"] == 1

    future = strict_sync(mock_world, detection_max_age=8.0)
    future._apply([value], now=media_epoch - 6.0)
    assert future.actor_ids() == set()
    assert future.status()["strict_rejections"]["strict_live_detection_future"] == 1

    replay = strict_sync(mock_world, detection_max_age=8.0)
    replay._apply([value], now=media_epoch, use_detection_ts=True)
    assert len(replay.actor_ids()) == 1


def test_blueprint_binding_and_actual_dimensions_fail_closed(mock_world):
    binding_mismatch = strict_sync(mock_world)
    forged = detection()
    forged["reviewed_localization"]["placement"]["blueprint"][
        "selected_blueprint_id"
    ] = "vehicle.not.in.runtime.pool"
    reseal(forged)
    binding_mismatch._apply([forged])
    assert binding_mismatch.actor_ids() == set()
    assert binding_mismatch.status()["strict_rejections"][
        "active_blueprint_binding_mismatch"
    ] == 1

    original_spawn = mock_world.try_spawn_actor

    def wrong_dimensions(*args, **kwargs):
        actor = original_spawn(*args, **kwargs)
        actor.bounding_box.extent.x = 9.0
        return actor

    mock_world.try_spawn_actor = wrong_dimensions
    dimensions_mismatch = strict_sync(mock_world)
    dimensions_mismatch._apply([detection()])
    assert dimensions_mismatch.actor_ids() == set()
    assert dimensions_mismatch.status()["strict_rejections"][
        "active_blueprint_dimensions_mismatch"
    ] == 1


@pytest.mark.parametrize("failure", ["false", "exception"])
def test_cleanup_failure_retains_ownership_and_surfaces_status(mock_world, failure):
    sync = strict_sync(mock_world)
    sync._apply([detection()])
    actor_id = next(iter(sync.actor_ids()))
    actor = mock_world.get_actor(actor_id)
    if failure == "false":
        actor.destroy = lambda: False
    else:
        def raise_destroy():
            raise RuntimeError("injected cleanup exception")
        actor.destroy = raise_destroy

    sync.stop()
    assert sync.actor_ids() == {actor_id}
    status = sync.status()
    assert status["cleanup_failures"]["global_car_reviewed"].startswith(
        "track_cleanup:destroy_"
    )
    assert status["objects"][0]["tracked_actor_id"] == actor_id


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
