import base64
import json
import re
import sys
import types
import unittest
from pathlib import Path


class Condition:
    def __init__(self, expression):
        self.expression = expression

    def __and__(self, other):
        return Condition(("and", self.expression, other.expression))


class Key:
    def __init__(self, name):
        self.name = name

    def eq(self, value):
        return Condition(("eq", self.name, value))

    def between(self, start, end):
        return Condition(("between", self.name, start, end))


class Attr(Key):
    pass


class FakeTable:
    def __init__(self):
        self.query_calls = []
        self.items = [
            {
                "object_id": "newest",
                "timestamp_utc": "2026-07-10T05:30:00.000Z",
                "fleet_id": "private",
            },
            {
                "object_id": "older",
                "timestamp_utc": "2026-07-10T05:29:00.000Z",
            },
        ]
        self.last_evaluated_key = {
            "object_id": "older",
            "ts_event": "2026-07-10T05:29:00.000Z#event",
        }

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return {
            "Items": self.items,
            "LastEvaluatedKey": self.last_evaluated_key,
        }

    def scan(self, **_kwargs):
        raise AssertionError("recent detections must not use DynamoDB Scan")


def generated_lambda_source():
    script = Path(__file__).resolve().parents[1] / "provision-read-api.sh"
    text = script.read_text(encoding="utf-8")
    match = re.search(
        r'cat > "\$\{WORKDIR\}/index\.py" <<PY\n(?P<source>.*?)\nPY\n',
        text,
        re.DOTALL,
    )
    if not match:
        raise AssertionError("could not extract generated read Lambda source")

    source = match.group("source")
    replacements = {
        "${TABLE_NAME}": "test-detections",
        "${VIDEO_AWS_REGION}": "us-west-2",
        "${VIDEO_STREAM_PREFIX}": "test-camera-",
        "${VIDEO_HLS_EXPIRES_SECONDS}": "300",
        "${VIDEO_ONDEMAND_EXPIRES_SECONDS}": "3600",
        "${SITE_GEOHASH}": "9q9p8",
        "${STATE_BUCKET}": "test-state",
        "${SNAPSHOT_URL_EXPIRES_SECONDS}": "300",
        "${DEMO_VIDEOS_PREFIX}": "demo-videos/",
        "${DEMO_VIDEO_URL_EXPIRES_SECONDS}": "3600",
    }
    for placeholder, value in replacements.items():
        source = source.replace(placeholder, value)
    if "${" in source:
        raise AssertionError("unexpanded shell placeholder in generated Lambda")
    return source


def load_generated_lambda(fake_table):
    boto3 = types.ModuleType("boto3")
    boto3.resource = lambda _service: types.SimpleNamespace(
        Table=lambda _name: fake_table
    )
    boto3.client = lambda *_args, **_kwargs: types.SimpleNamespace()

    conditions = types.ModuleType("boto3.dynamodb.conditions")
    conditions.Attr = Attr
    conditions.Key = Key

    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = lambda **kwargs: kwargs
    botocore_exceptions = types.ModuleType("botocore.exceptions")
    botocore_exceptions.ClientError = type("ClientError", (Exception,), {})

    previous = {
        name: sys.modules.get(name)
        for name in (
            "boto3",
            "boto3.dynamodb",
            "boto3.dynamodb.conditions",
            "botocore",
            "botocore.config",
            "botocore.exceptions",
        )
    }
    sys.modules["boto3"] = boto3
    sys.modules["boto3.dynamodb"] = types.ModuleType("boto3.dynamodb")
    sys.modules["boto3.dynamodb.conditions"] = conditions
    sys.modules["botocore"] = types.ModuleType("botocore")
    sys.modules["botocore.config"] = botocore_config
    sys.modules["botocore.exceptions"] = botocore_exceptions
    try:
        namespace = {"__name__": "generated_read_api"}
        exec(compile(generated_lambda_source(), "generated-index.py", "exec"), namespace)
        return namespace
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class RecentDetectionsTest(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        self.module = load_generated_lambda(self.table)

    def invoke(self, next_token=None):
        query = {"limit": "2"}
        if next_token:
            query["next"] = next_token
        response = self.module["handler"](
            {
                "rawPath": "/detections/recent",
                "queryStringParameters": query,
            },
            None,
        )
        self.assertEqual(response["statusCode"], 200)
        return json.loads(response["body"])

    def test_recent_queries_site_time_index_newest_first(self):
        body = self.invoke()
        call = self.table.query_calls[-1]
        self.assertEqual(call["IndexName"], "gsi_geohash_time")
        self.assertEqual(call["Limit"], 2)
        self.assertIs(call["ScanIndexForward"], False)
        self.assertEqual(
            call["KeyConditionExpression"].expression,
            ("eq", "geohash", "9q9p8"),
        )
        self.assertEqual([item["object_id"] for item in body["items"]], ["newest", "older"])
        self.assertNotIn("fleet_id", body["items"][0])

    def test_recent_pagination_round_trips_last_evaluated_key(self):
        first = self.invoke()
        decoded = json.loads(base64.urlsafe_b64decode(first["next"]).decode("utf-8"))
        self.assertEqual(decoded, self.table.last_evaluated_key)

        self.invoke(first["next"])
        self.assertEqual(
            self.table.query_calls[-1]["ExclusiveStartKey"],
            self.table.last_evaluated_key,
        )


class DetectionTimelineTrustTest(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        self.table.last_evaluated_key = None
        self.table.items = [
            {
                "event_id": "trusted-event",
                "object_id": "global_car_run_1",
                "object_type": "car",
                "timestamp_utc": "2026-07-10T05:30:00.000Z",
                "media_timestamp_utc": "2026-07-10T05:30:00.000Z",
                "timestamp_schema_version": 2,
                "media_time_trusted": True,
                "media_clock": {
                    "source": "hls_ext_x_program_date_time",
                    "schema_version": 1,
                    "anchor_program_date_time_utc": "2026-07-10T05:29:59.000Z",
                    "position_milliseconds": 1000.0,
                },
                "device_id": "ch1",
                "confidence_score": 0.9,
            },
            {
                "event_id": "legacy-event",
                "object_id": "global_car_legacy_1",
                "object_type": "car",
                "timestamp_utc": "2026-07-10T05:31:00.000Z",
                "device_id": "ch4",
                "confidence_score": 0.8,
            },
            {
                "event_id": "mismatched-event",
                "object_id": "global_car_timestamp_mismatch_1",
                "object_type": "car",
                "timestamp_utc": "2026-07-10T05:32:00.000Z",
                "media_timestamp_utc": "2026-07-10T04:32:00.000Z",
                "timestamp_schema_version": 2,
                "media_time_trusted": True,
                "media_clock": {
                    "source": "hls_ext_x_program_date_time",
                    "schema_version": 1,
                    "anchor_program_date_time_utc": "2026-07-10T04:31:59.000Z",
                    "position_milliseconds": 1000.0,
                },
                "device_id": "ch1",
                "confidence_score": 0.7,
            },
            {
                "event_id": "boolean-schema-event",
                "object_id": "global_car_boolean_schema_1",
                "object_type": "car",
                "timestamp_utc": "2026-07-10T05:33:00.000Z",
                "media_timestamp_utc": "2026-07-10T05:33:00.000Z",
                "timestamp_schema_version": 2,
                "media_time_trusted": True,
                "media_clock": {
                    "source": "hls_ext_x_program_date_time",
                    "schema_version": True,
                    "anchor_program_date_time_utc": "2026-07-10T05:32:59.000Z",
                    "position_milliseconds": 1000.0,
                },
                "device_id": "ch1",
                "confidence_score": 0.7,
            },
            {
                "event_id": "spoofed-event",
                "object_id": "global_car_schema_spoof_1",
                "object_type": "car",
                "timestamp_utc": "2026-07-10T05:34:00.000Z",
                "media_timestamp_utc": "2026-07-10T05:34:00.000Z",
                "timestamp_schema_version": 2,
                "media_time_trusted": True,
                "media_clock": {
                    "source": "hls_ext_x_program_date_time",
                    "schema_version": 1,
                },
                "device_id": "ch1",
                "confidence_score": 0.7,
            },
        ]
        self.module = load_generated_lambda(self.table)

    def test_timeline_labels_only_strict_schema_v2_media_events_trusted(self):
        response = self.module["handler"](
            {
                "rawPath": "/detections/timeline",
                "queryStringParameters": {
                    "start": "2026-07-10T05:00:00.000Z",
                    "end": "2026-07-10T06:00:00.000Z",
                },
            },
            None,
        )
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        events = {event["object_id"]: event for event in body["events"]}
        self.assertIs(events["global_car_run_1"]["media_time_trusted"], True)
        self.assertEqual(
            events["global_car_run_1"]["timestamp_schema_version"], 2
        )
        self.assertEqual(
            events["global_car_run_1"]["first_event_id"], "trusted-event"
        )
        self.assertIs(events["global_car_legacy_1"]["media_time_trusted"], False)
        self.assertIs(
            events["global_car_timestamp_mismatch_1"]["media_time_trusted"],
            False,
        )
        self.assertIs(
            events["global_car_boolean_schema_1"]["media_time_trusted"],
            False,
        )
        self.assertIs(
            events["global_car_schema_spoof_1"]["media_time_trusted"],
            False,
        )
        projection = self.table.query_calls[-1]["ProjectionExpression"]
        self.assertIn("media_clock", projection)
        self.assertIn("media_time_trusted", projection)


if __name__ == "__main__":
    unittest.main()
