import json

import pytest
import tools.verify_phase4_live as live_probe

from tools.verify_phase4_live import (
    VerificationError,
    apply_probe,
    binary_digest,
    build_parser,
    choose_teleport_target,
    project_world_xyz,
    receive_binary_frame,
    receive_json,
    replay_clock_epoch,
    session_candidate_actor_ids,
    synchronize_world,
    teleport_pose_errors,
    validate_actor_delta,
    validate_cli_args,
    validate_isolated_ego_roles,
    validate_session_actor_manifest,
    validate_twin_camera_model,
    validate_projected_actor_detection,
    validate_twin_object_sample,
    validate_twin_object_samples,
    validate_zero_active_sessions,
    verify_twin,
    websocket_url,
)


def twin_camera_hello(camera_id="ch1"):
    return {
        "type": "twin_hello",
        "camera_id": camera_id,
        "width": 1280,
        "height": 960,
        "camera_model": {
            "camera_id": camera_id,
            "actor_id": 33,
            "config_sha256": "a" * 64,
            "transform": {
                "location": {"x": 1.0, "y": 2.0, "z": 8.0},
                "rotation": {"pitch": -35.0, "yaw": 90.0, "roll": 0.0},
            },
            "image": {
                "width": 1280,
                "height": 960,
                "horizontal_fov_deg": 90.0,
            },
            "lens": {"lens_k": 0.0, "lens_kcube": 0.0},
        },
    }


class FakeLocation:
    def __init__(self, x, y, z=0.0):
        self.x = x
        self.y = y
        self.z = z


class FakeRotation:
    def __init__(self, pitch=0.0, yaw=0.0, roll=0.0):
        self.pitch = pitch
        self.yaw = yaw
        self.roll = roll


class FakeTransform:
    def __init__(self, x, y, z=0.0, pitch=0.0, yaw=0.0, roll=0.0):
        self.location = FakeLocation(x, y, z)
        self.rotation = FakeRotation(pitch, yaw, roll)


class FakeMap:
    def __init__(self, points):
        self._points = points

    def get_spawn_points(self):
        return self._points


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = iter(messages)

    async def recv(self):
        return next(self.messages)


class FakeActor:
    def __init__(
        self,
        actor_id=77,
        type_id="vehicle.tesla.model3",
        role_name="twin_object",
        transform=None,
    ):
        self.id = actor_id
        self.type_id = type_id
        self.attributes = {"role_name": role_name}
        self._transform = transform or FakeTransform(10.0, 20.0, 0.3, yaw=12.0)

    def get_transform(self):
        return self._transform


class FakeWorld:
    def __init__(self, actors=()):
        self.actors = {actor.id: actor for actor in actors}

    def get_actor(self, actor_id):
        return self.actors.get(actor_id)


def twin_status(
    *,
    clock="2026-07-10T06:00:00.000Z",
    object_id="global_car_run_1",
    actor_id=77,
    object_type="car",
    actor_type="vehicle.tesla.model3",
    x=10.0,
    y=20.0,
):
    return {
        "type": "twin_mode",
        "mode": "replay",
        "replay_clock": clock,
        "objects": [
            {
                "object_id": object_id,
                "object_type": object_type,
                "event_id": "event-1",
                "detection_timestamp_utc": clock,
                "media_timestamp_utc": clock,
                "timestamp_schema_version": 2,
                "media_time_trusted": True,
                "media_clock": {
                    "schema_version": 1,
                    "source": "hls_ext_x_program_date_time",
                    "anchor_program_date_time_utc": clock,
                    "position_milliseconds": 0,
                },
                "actor_id": actor_id,
                "actor_present": True,
                "actor_type": actor_type,
                "carla_transform": {
                    "location": {"x": x, "y": y, "z": 0.3},
                    "rotation": {"pitch": 0.0, "yaw": 12.0, "roll": 0.0},
                },
            }
        ],
    }


def test_validates_fingerprinted_twin_camera_model():
    model = validate_twin_camera_model(twin_camera_hello(), "ch1")
    assert model["camera_id"] == "ch1"
    assert model["actor_id"] == 33
    assert model["image"]["horizontal_fov_deg"] == pytest.approx(90.0)


@pytest.mark.parametrize(
    "mutate,error",
    [
        (lambda hello: hello.pop("camera_model"), "no matching camera model"),
        (
            lambda hello: hello["camera_model"].update(config_sha256="bad"),
            "config fingerprint",
        ),
        (
            lambda hello: hello["camera_model"]["image"].update(width=640),
            "image geometry",
        ),
    ],
)
def test_rejects_unverifiable_twin_camera_model(mutate, error):
    hello = twin_camera_hello()
    mutate(hello)
    with pytest.raises(VerificationError, match=error):
        validate_twin_camera_model(hello, "ch1")


def test_projects_world_point_through_fingerprinted_camera_model():
    model = twin_camera_hello()["camera_model"]
    model["transform"] = {
        "location": {"x": 0.0, "y": 0.0, "z": 0.0},
        "rotation": {"pitch": 0.0, "yaw": 0.0, "roll": 0.0},
    }
    model["image"] = {
        "width": 1000,
        "height": 500,
        "horizontal_fov_deg": 90.0,
    }
    assert project_world_xyz((10.0, 0.0, 0.0), model) == pytest.approx(
        (500.0, 250.0, 10.0)
    )
    assert project_world_xyz((10.0, 1.0, 1.0), model) == pytest.approx(
        (550.0, 200.0, 10.0)
    )
    model["lens"]["lens_k"] = -0.1
    with pytest.raises(VerificationError, match="nonzero CARLA lens distortion"):
        project_world_xyz((10.0, 0.0, 0.0), model)


def test_projected_actor_requires_strong_compatible_visual_overlap():
    result = validate_projected_actor_detection(
        (100.0, 100.0, 200.0, 200.0),
        [
            {"label": "person", "confidence": 0.99, "bbox": [100, 100, 200, 200]},
            {"label": "car", "confidence": 0.90, "bbox": [110, 110, 195, 195]},
        ],
        "car",
    )
    assert result["best_detection"]["compatible"] is True
    assert result["best_detection"]["iou_with_projected_actor"] > 0.7
    with pytest.raises(VerificationError, match="no compatible visual detection"):
        validate_projected_actor_detection(
            (100.0, 100.0, 200.0, 200.0),
            [{"label": "car", "confidence": 0.9, "bbox": [190, 190, 260, 260]}],
            "car",
        )


def twin_sample(clock, x, *, actor_id=77):
    return {
        "object_id": "global_car_run_1",
        "actor_id": actor_id,
        "event_id": f"event-{int(clock)}",
        "media_timestamp_epoch": clock,
        "replay_clock_epoch": clock,
        "reported_transform": {
            "location": {"x": x, "y": 20.0, "z": 0.3},
            "rotation": {"pitch": 0.0, "yaw": 12.0, "roll": 0.0},
        },
        "visual": {
            "frame_sha256": f"{int(clock):064x}",
            "best_detection": {"compatible": True},
        },
    }


def test_websocket_url_replaces_existing_path_and_query():
    assert websocket_url("wss://example.test/old?stale=1", "/twin", "control=1") == (
        "wss://example.test/twin?control=1"
    )


def test_choose_teleport_target_maximizes_clearance():
    near = FakeTransform(5.0, 0.0)
    far = FakeTransform(100.0, 100.0)
    target = choose_teleport_target(FakeMap([near, far]), [[0.0, 0.0, 0.0]])
    assert target is far


def test_choose_teleport_target_requires_safe_separation():
    with pytest.raises(RuntimeError, match="safely separated"):
        choose_teleport_target(
            FakeMap([FakeTransform(1.0, 1.0)]), [[0.0, 0.0, 0.0]]
        )


def test_role_validation_matches_drive_session_prefix():
    validate_isolated_ego_roles(["ego_vehicle_abc", "ego_vehicle_def"])


@pytest.mark.parametrize(
    "roles",
    [
        ["v2x_ego_abc", "v2x_ego_def"],
        ["ego_vehicle_same", "ego_vehicle_same"],
        ["ego_vehicle_only"],
    ],
)
def test_role_validation_rejects_wrong_or_overlapping_roles(roles):
    with pytest.raises(RuntimeError, match="not isolated"):
        validate_isolated_ego_roles(roles)


@pytest.mark.asyncio
async def test_receive_json_skips_binary_and_unrelated_messages():
    evidence = {}
    websocket = FakeWebSocket(
        [
            b"jpeg",
            "not json",
            json.dumps({"type": "telemetry"}),
            json.dumps({"type": "server_status", "active_sessions": 0}),
        ]
    )
    result = await receive_json(
        websocket, {"server_status"}, timeout=1.0, evidence=evidence
    )
    assert result["active_sessions"] == 0
    assert evidence["binary_frames"] == 1
    assert evidence["json_types"] == ["telemetry", "server_status"]


@pytest.mark.asyncio
async def test_receive_binary_frame_requires_changed_replay_content():
    evidence = {}
    baseline = binary_digest(b"live-frame")
    websocket = FakeWebSocket(
        [
            b"live-frame",
            json.dumps({"type": "twin_clock"}),
            b"replay-frame",
        ]
    )

    digest = await receive_binary_frame(
        websocket,
        timeout=1.0,
        evidence=evidence,
        different_from=baseline,
    )

    assert digest == binary_digest(b"replay-frame")
    assert evidence["binary_frames"] == 2
    assert evidence["json_types"] == ["twin_clock"]


def test_replay_clock_normalizes_live_iso_protocol_value():
    assert replay_clock_epoch("2026-07-09T22:11:12.500Z") == pytest.approx(
        1_783_635_072.5
    )
    assert replay_clock_epoch("not-a-time") is None
    assert replay_clock_epoch(float("inf")) is None


def test_actor_manifest_requires_existing_exact_disjoint_categories():
    response = {
        "vehicle_id": 10,
        "sensor_actor_ids": [11, 12],
        "scene_actor_ids": [13],
        "owned_actor_ids": [10, 11, 12, 13],
    }

    vehicle, owned, sensors, scene = validate_session_actor_manifest(
        response,
        baseline_actor_ids={1, 2},
        current_actor_ids={1, 2, 10, 11, 12, 13},
        prior_owned_actor_ids={20},
    )

    assert vehicle == 10
    assert owned == {10, 11, 12, 13}
    assert sensors == {11, 12}
    assert scene == {13}

    broken = {**response, "owned_actor_ids": [10, 11, 13]}
    with pytest.raises(VerificationError, match="exactly ego"):
        validate_session_actor_manifest(
            broken,
            baseline_actor_ids={1, 2},
            current_actor_ids={1, 2, 10, 11, 12, 13},
            prior_owned_actor_ids=set(),
        )


def test_full_actor_delta_rejects_unmanifested_or_nonexistent_actor():
    validate_actor_delta({10, 11, 12}, {10, 11, 12})
    with pytest.raises(VerificationError, match=r"undeclared=\[99\]"):
        validate_actor_delta({10, 11, 12, 99}, {10, 11, 12})
    with pytest.raises(VerificationError, match=r"missing=\[12\]"):
        validate_actor_delta({10, 11}, {10, 11, 12})


def test_actor_delta_allows_only_known_map_and_live_twin_actors():
    inventory = {
        15: {"type_id": "spectator", "role_name": ""},
        18: {"type_id": "traffic.traffic_light", "role_name": ""},
        33: {"type_id": "sensor.camera.rgb", "role_name": "twin_rig"},
        40: {"type_id": "walker.pedestrian.0001", "role_name": "twin_object"},
        41: {"type_id": "vehicle.tesla.model3", "role_name": "ego_vehicle_a"},
        42: {"type_id": "sensor.camera.rgb", "role_name": ""},
    }

    ignored = validate_actor_delta(set(inventory), {41, 42}, inventory)

    assert ignored == {15, 18, 33, 40}


@pytest.mark.parametrize(
    "identity",
    [
        {"type_id": "sensor.camera.rgb", "role_name": ""},
        {"type_id": "static.prop.trafficcone01", "role_name": ""},
        {"type_id": "vehicle.tesla.model3", "role_name": "autopilot"},
        {"type_id": "vehicle.tesla.model3", "role_name": "twin_rig"},
        {"type_id": "sensor.camera.rgb", "role_name": "twin_object"},
    ],
)
def test_actor_delta_still_rejects_possible_drive_session_leaks(identity):
    with pytest.raises(VerificationError, match=r"undeclared=\[99\]"):
        validate_actor_delta({99}, set(), {99: identity})


def test_cleanup_candidates_exclude_map_and_live_twin_churn():
    inventory = {
        15: {"type_id": "spectator", "role_name": ""},
        33: {"type_id": "sensor.camera.rgb", "role_name": "twin_rig"},
        40: {"type_id": "vehicle.audi.tt", "role_name": "twin_object"},
        41: {"type_id": "vehicle.tesla.model3", "role_name": "ego_vehicle_a"},
        42: {"type_id": "static.prop.trafficcone01", "role_name": ""},
    }

    assert session_candidate_actor_ids(set(inventory), inventory) == {41, 42}


def test_world_synchronization_requires_a_real_nonzero_snapshot():
    class Snapshot:
        frame = 1234

    class World:
        def __init__(self):
            self.timeout = None

        def wait_for_tick(self, timeout):
            self.timeout = timeout
            return Snapshot()

    world = World()
    assert synchronize_world(world, 2.5) == 1234
    assert world.timeout == 2.5


@pytest.mark.parametrize("snapshot", [None, object()])
def test_world_synchronization_rejects_missing_or_frame_zero_snapshot(snapshot):
    class World:
        def wait_for_tick(self, _timeout):
            return snapshot

    with pytest.raises(VerificationError, match="no real frame"):
        synchronize_world(World(), 1.0)


def test_teleport_pose_errors_compare_requested_target_and_wrap_yaw():
    position_error, yaw_error = teleport_pose_errors(
        [3.0, 4.0, 99.0], 179.0, [0.0, 0.0], -179.0
    )

    assert position_error == pytest.approx(5.0)
    assert yaw_error == pytest.approx(2.0)


@pytest.mark.parametrize(
    ("position", "yaw"),
    [
        ([], 0.0),
        ([0.0, 0.0], None),
        ([float("nan"), 0.0], 0.0),
    ],
)
def test_teleport_pose_errors_reject_malformed_protocol_values(position, yaw):
    with pytest.raises(VerificationError, match="no valid pose"):
        teleport_pose_errors(position, yaw, [0.0, 0.0], 0.0)


def test_exact_twin_sample_proves_actor_role_type_and_transform():
    actor = FakeActor()
    sample = validate_twin_object_sample(
        twin_status(),
        "global_car_run_1",
        FakeWorld([actor]),
        position_tolerance_m=0.1,
        rotation_tolerance_deg=0.1,
    )

    assert sample["actor_id"] == actor.id
    assert sample["actor_type"] == actor.type_id
    assert sample["role_name"] == "twin_object"
    assert sample["position_error_m"] == 0.0
    assert sample["rotation_error_deg"] == 0.0


@pytest.mark.parametrize(
    ("actor", "status", "message"),
    [
        (FakeActor(role_name="autopilot"), twin_status(), "unexpected role"),
        (
            FakeActor(type_id="walker.pedestrian.0001"),
            twin_status(actor_type="walker.pedestrian.0001"),
            "incompatible",
        ),
        (
            FakeActor(transform=FakeTransform(13.0, 20.0, 0.3, yaw=12.0)),
            twin_status(),
            "transform does not match",
        ),
    ],
)
def test_exact_twin_sample_rejects_wrong_actor_evidence(actor, status, message):
    with pytest.raises(VerificationError, match=message):
        validate_twin_object_sample(
            status,
            "global_car_run_1",
            FakeWorld([actor]),
            position_tolerance_m=0.5,
            rotation_tolerance_deg=1.0,
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda item: item.update(actor_present=False), "present CARLA actor"),
        (lambda item: item.update(timestamp_schema_version=True), "schema is not version 2"),
        (
            lambda item: item.update(detection_timestamp_utc="2026-07-10T05:00:00Z"),
            "timestamps disagree",
        ),
        (
            lambda item: item["media_clock"].update(position_milliseconds=True),
            "invalid media clock provenance",
        ),
    ],
)
def test_exact_twin_sample_rejects_untrusted_media_or_absent_actor(mutate, message):
    status = twin_status()
    mutate(status["objects"][0])

    with pytest.raises(VerificationError, match=message):
        validate_twin_object_sample(
            status,
            "global_car_run_1",
            FakeWorld([FakeActor()]),
            position_tolerance_m=0.5,
            rotation_tolerance_deg=1.0,
        )


def test_twin_samples_require_three_stable_samples_over_two_seconds_with_motion():
    summary = validate_twin_object_samples(
        [twin_sample(100.0, 10.0), twin_sample(101.0, 10.15), twin_sample(102.0, 10.4)]
    )

    assert summary == {
        "sample_count": 3,
        "object_id": "global_car_run_1",
        "actor_id": 77,
        "event_ids": ["event-100", "event-101", "event-102"],
        "media_start": "1970-01-01T00:01:40.000Z",
        "media_end": "1970-01-01T00:01:42.000Z",
        "replay_span_seconds": 2.0,
        "max_planar_movement_m": 0.4,
        "planar_path_length_m": 0.4,
        "visual_frame_sha256": [
            f"{value:064x}" for value in (100, 101, 102)
        ],
    }


@pytest.mark.parametrize(
    ("samples", "message"),
    [
        ([twin_sample(100.0, 10.0), twin_sample(102.0, 10.4)], "only 2 samples"),
        (
            [
                twin_sample(100.0, 10.0),
                twin_sample(101.0, 10.2, actor_id=88),
                twin_sample(102.0, 10.4),
            ],
            "do not retain",
        ),
        (
            [twin_sample(100.0, 10.0), twin_sample(101.0, 10.2), twin_sample(101.0, 10.4)],
            "did not advance",
        ),
        (
            [twin_sample(100.0, 10.0), twin_sample(100.9, 10.2), twin_sample(101.9, 10.4)],
            "span only",
        ),
        (
            [twin_sample(100.0, 10.0), twin_sample(101.0, 10.05), twin_sample(102.0, 10.1)],
            "moved only",
        ),
    ],
)
def test_twin_samples_reject_weak_identity_time_or_motion_proof(samples, message):
    with pytest.raises(VerificationError, match=message):
        validate_twin_object_samples(samples)


def test_twin_samples_reject_reused_rendered_frame():
    samples = [
        twin_sample(100.0, 10.0),
        twin_sample(101.0, 10.2),
        twin_sample(102.0, 10.5),
    ]
    for sample in samples:
        sample["visual"]["frame_sha256"] = "a" * 64
    with pytest.raises(VerificationError, match="reused a rendered frame"):
        validate_twin_object_samples(samples)


def test_zero_active_session_gate_rejects_busy_or_malformed_status():
    assert validate_zero_active_sessions({"active_sessions": 0}) == {
        "active_sessions": 0
    }
    with pytest.raises(VerificationError, match="another Drive session"):
        validate_zero_active_sessions({"active_sessions": 1})
    with pytest.raises(VerificationError, match="no valid"):
        validate_zero_active_sessions({"active_sessions": False})


def test_exact_object_cli_supports_camera_replay_start_and_skip_drive(tmp_path):
    model = tmp_path / "model.pt"
    model.write_bytes(b"weights")
    args = validate_cli_args(
        build_parser().parse_args(
            [
                "--apply",
                "--skip-drive",
                "--twin-object-id",
                "global_car_run_1",
                "--twin-replay-start",
                "2026-07-10T06:00:00Z",
                "--twin-camera",
                "ch4",
                "--twin-yolo-model",
                str(model),
            ]
        )
    )

    assert args.skip_drive is True
    assert args.twin_object_id == "global_car_run_1"
    assert args.twin_replay_start == "2026-07-10T06:00:00Z"
    assert args.twin_camera == "ch4"

    with pytest.raises(VerificationError, match="only with --apply"):
        validate_cli_args(build_parser().parse_args(["--skip-drive"]))


@pytest.mark.asyncio
async def test_apply_skip_drive_keeps_zero_session_gate_without_starting_sessions(
    monkeypatch,
):
    calls = []

    async def fake_zero_gate(_args):
        calls.append("zero_gate")
        return {"type": "server_status", "active_sessions": 0}

    async def forbidden_drive_sessions(*_args, **_kwargs):
        raise AssertionError("--skip-drive must not create Drive sessions")

    monkeypatch.setattr(live_probe, "verify_zero_active_sessions", fake_zero_gate)
    monkeypatch.setattr(live_probe, "verify_drive_sessions", forbidden_drive_sessions)

    class World:
        class Map:
            name = "Richmond_Field_Station_Richmond_CA"

        def get_map(self):
            return self.Map()

    class Client:
        def __init__(self, _host, _port):
            self.world = World()

        def set_timeout(self, _timeout):
            pass

        def get_world(self):
            return self.world

    CarlaModule = type("CarlaModule", (), {"Client": Client})

    args = validate_cli_args(
        build_parser().parse_args(["--apply", "--skip-drive", "--skip-twin"])
    )
    result = await apply_probe(args, carla_module=CarlaModule)

    assert calls == ["zero_gate"]
    assert result["preflight_server_status"]["active_sessions"] == 0
    assert result["drive"]["skipped"] is True


@pytest.mark.asyncio
async def test_twin_replay_restores_live_when_acceptance_fails(monkeypatch):
    class Context:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(live_probe.websockets, "connect", lambda *_a, **_k: Context())

    async def fake_receive_json(*_args, **_kwargs):
        return twin_camera_hello()

    binary_calls = 0

    async def fake_receive_binary(*_args, **_kwargs):
        nonlocal binary_calls
        binary_calls += 1
        if binary_calls == 1:
            return "live-digest"
        raise VerificationError("injected replay-frame failure")

    sent_types = []

    async def fake_request_json(_socket, payload, *_args, **_kwargs):
        sent_types.append(payload["type"])
        if payload["type"] == "twin_status":
            return {"type": "twin_mode", "mode": "live", "replay_supported": True}
        if payload["type"] == "twin_replay":
            return {"type": "twin_mode", "mode": "replay"}
        if payload["type"] == "twin_live":
            return {"type": "twin_mode", "mode": "live"}
        raise AssertionError(payload)

    monkeypatch.setattr(live_probe, "receive_json", fake_receive_json)
    monkeypatch.setattr(live_probe, "receive_binary_frame", fake_receive_binary)
    monkeypatch.setattr(live_probe, "request_json", fake_request_json)

    args = validate_cli_args(
        build_parser().parse_args(["--apply", "--skip-drive"])
    )
    with pytest.raises(VerificationError, match="injected replay-frame failure"):
        await verify_twin(args)

    assert sent_types[-1] == "twin_live"


@pytest.mark.asyncio
async def test_exact_twin_samples_synchronize_independent_carla_client(monkeypatch):
    actor = FakeActor()
    world = FakeWorld([actor])
    statuses = [
        twin_status(clock="2026-07-10T06:00:00.000Z", x=10.0),
        twin_status(clock="2026-07-10T06:00:01.000Z", x=10.2),
        twin_status(clock="2026-07-10T06:00:02.000Z", x=10.5),
    ]
    call_order = []
    sync_frames = iter((101, 102, 103))

    def fake_synchronize(sync_world, timeout):
        assert sync_world is world
        assert 0.0 < timeout <= 1.0
        call_order.append("sync")
        return next(sync_frames)

    async def fake_request_json(*_args, **_kwargs):
        call_order.append("request")
        status = statuses.pop(0)
        location = status["objects"][0]["carla_transform"]["location"]
        actor._transform = FakeTransform(
            location["x"], location["y"], location["z"], yaw=12.0
        )
        return status

    monkeypatch.setattr(live_probe, "synchronize_world", fake_synchronize)
    monkeypatch.setattr(live_probe, "request_json", fake_request_json)
    monkeypatch.setattr(
        live_probe, "project_actor_bbox", lambda *_args: (100.0, 100.0, 200.0, 200.0)
    )

    async def fake_binary(*_args, **_kwargs):
        fake_binary.count += 1
        return b"jpeg", f"{fake_binary.count:064x}"

    fake_binary.count = 0

    monkeypatch.setattr(live_probe, "receive_binary_payload", fake_binary)
    monkeypatch.setattr(
        live_probe,
        "detect_twin_objects",
        lambda *_args, **_kwargs: [
            {"label": "car", "confidence": 0.9, "bbox": [100, 100, 200, 200]}
        ],
    )

    args = type(
        "Args",
        (),
        {
            "timeout": 5.0,
            "twin_object_id": "global_car_run_1",
            "position_tolerance_m": 0.5,
            "yaw_tolerance_deg": 1.0,
            "twin_yolo_confidence": 0.25,
            "twin_yolo_device": "cpu",
            "twin_min_iou": 0.15,
            "twin_min_actor_coverage": 0.5,
        },
    )()
    evidence = {}

    result = await live_probe.collect_twin_object_samples(
        args, object(), object(), world, twin_camera_hello()["camera_model"], object(), evidence
    )

    assert call_order == ["sync", "request"] * 3
    assert evidence["object_sync_frames"] == [101, 102, 103]
    assert result["sample_count"] == 3
    assert result["max_planar_movement_m"] == pytest.approx(0.5)
    assert all(sample["visual"]["best_detection"]["compatible"] for sample in result["samples"])
