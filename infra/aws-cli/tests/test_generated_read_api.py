import base64
import io
import json
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


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


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.delete_calls = []

    def put_object(self, **kwargs):
        body = kwargs["Body"]
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.objects[kwargs["Key"]] = bytes(body)
        self.put_calls.append(kwargs)
        return {}

    def get_object(self, **kwargs):
        try:
            body = self.objects[kwargs["Key"]]
        except KeyError as exc:
            raise FakeClientError("NoSuchKey") from exc
        return {"Body": io.BytesIO(body)}

    def delete_object(self, **kwargs):
        self.objects.pop(kwargs["Key"], None)
        self.delete_calls.append(kwargs)
        return {}


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
    root = Path(__file__).resolve().parents[1]
    source_path = root / "read-api-lambda.py"
    script = (root / "provision-read-api.sh").read_text(encoding="utf-8")
    expected_install = 'install -m 0600 "${HERE}/read-api-lambda.py" "${WORKDIR}/index.py"'
    if script.count(expected_install) != 1:
        raise AssertionError("deployment does not package the tested Lambda source")
    source = source_path.read_text(encoding="utf-8")
    if "${" in source:
        raise AssertionError("shell interpolation is forbidden in Lambda source")
    compile(source, str(source_path), "exec")
    return source


class DeploymentArtifactContractTest(unittest.TestCase):
    def test_exact_source_is_compiled_before_packaging_and_iam(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "provision-read-api.sh").read_text(encoding="utf-8")
        install_at = script.index(
            'install -m 0600 "${HERE}/read-api-lambda.py" "${WORKDIR}/index.py"'
        )
        compile_at = script.index('python3 -m py_compile "${WORKDIR}/index.py"')
        zip_at = script.index('zip -Xq function.zip index.py')
        lambda_apply_at = min(
            value
            for value in (
                script.find("aws lambda update-function-code", zip_at),
                script.find("aws lambda create-function", zip_at),
            )
            if value >= 0
        )
        iam_apply_at = script.index("aws iam put-role-policy", lambda_apply_at)
        self.assertLess(install_at, compile_at)
        self.assertLess(compile_at, zip_at)
        self.assertLess(zip_at, lambda_apply_at)
        self.assertLess(lambda_apply_at, iam_apply_at)
        self.assertNotIn("<<PY", script)

    def test_existing_lambda_configuration_is_reconciled_before_new_code(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "provision-read-api.sh").read_text(encoding="utf-8")
        existing_at = script.index('if [[ "${READ_LAMBDA_EXISTS}" == "true" ]]')
        configuration_at = script.index(
            "aws lambda update-function-configuration", existing_at
        )
        code_at = script.index("aws lambda update-function-code", existing_at)
        self.assertLess(configuration_at, code_at)

    def test_proxy_state_has_lifecycle_and_session_mint_throttle(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "provision-read-api.sh").read_text(encoding="utf-8")
        self.assertIn('HLS_PROXY_LIFECYCLE_RULE_ID="v2x-hls-proxy-expiry-v1"', script)
        self.assertIn("aws s3api get-bucket-lifecycle-configuration", script)
        self.assertIn("aws s3api put-bucket-lifecycle-configuration", script)
        self.assertIn('map(select(.ID != $rule_id))', script)
        self.assertIn('--arg direct_route_key "GET /video/session/{camera_id}"', script)
        self.assertIn('--arg browser_route_key "GET /video/browser-session/{camera_id}"', script)
        self.assertIn("ThrottlingBurstLimit", script)
        self.assertIn("ThrottlingRateLimit", script)


TEST_ENVIRONMENT = {
    "TABLE_NAME": "test-detections",
    "VIDEO_AWS_REGION": "us-west-2",
    "VIDEO_STREAM_PREFIX": "test-camera-",
    "VIDEO_HLS_EXPIRES_SECONDS": "300",
    "VIDEO_ONDEMAND_EXPIRES_SECONDS": "3600",
    "HLS_PROXY_PREFIX": "hls-proxy/v1/",
    "HLS_PROXY_FETCH_TIMEOUT_SECONDS": "8",
    "HLS_PROXY_PLAYLIST_MAX_BYTES": "1048576",
    "HLS_PROXY_SEGMENT_MAX_BYTES": "4194304",
    "SITE_GEOHASH": "9q9p8",
    "STATE_BUCKET": "test-state",
    "SNAPSHOT_URL_EXPIRES_SECONDS": "300",
    "DEMO_VIDEOS_PREFIX": "demo-videos/",
    "DEMO_VIDEO_URL_EXPIRES_SECONDS": "3600",
}


def load_generated_lambda(fake_table, fake_s3=None, environment=None):
    fake_s3 = fake_s3 or FakeS3()
    boto3 = types.ModuleType("boto3")
    boto3.resource = lambda _service: types.SimpleNamespace(
        Table=lambda _name: fake_table
    )
    boto3.client = lambda service, *_args, **_kwargs: (
        fake_s3 if service == "s3" else types.SimpleNamespace()
    )

    conditions = types.ModuleType("boto3.dynamodb.conditions")
    conditions.Attr = Attr
    conditions.Key = Key

    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = lambda **kwargs: kwargs
    botocore_exceptions = types.ModuleType("botocore.exceptions")
    botocore_exceptions.ClientError = FakeClientError

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
        with patch.dict(
            os.environ,
            TEST_ENVIRONMENT if environment is None else environment,
            clear=True,
        ):
            exec(compile(generated_lambda_source(), "generated-index.py", "exec"), namespace)
        return namespace
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class DeploymentCompatibilityTest(unittest.TestCase):
    def test_new_proxy_settings_have_safe_old_environment_defaults(self):
        old_environment = dict(TEST_ENVIRONMENT)
        for name in (
            "HLS_PROXY_PREFIX",
            "HLS_PROXY_FETCH_TIMEOUT_SECONDS",
            "HLS_PROXY_PLAYLIST_MAX_BYTES",
            "HLS_PROXY_SEGMENT_MAX_BYTES",
        ):
            old_environment.pop(name)
        module = load_generated_lambda(
            FakeTable(), FakeS3(), environment=old_environment
        )
        self.assertEqual(module["HLS_PROXY_PREFIX"], "hls-proxy/v1/")
        self.assertEqual(module["HLS_PROXY_FETCH_TIMEOUT_SECONDS"], 8)
        self.assertEqual(module["HLS_PROXY_PLAYLIST_MAX_BYTES"], 1048576)
        self.assertEqual(module["HLS_PROXY_SEGMENT_MAX_BYTES"], 4194304)


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


class LiveVideoSessionTest(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.module = load_generated_lambda(FakeTable(), self.s3)
        self.archived = types.SimpleNamespace()
        self.archived.calls = []
        self.secret = "s" * 64

        def session(**kwargs):
            self.archived.calls.append(kwargs)
            return {
                "HLSStreamingSessionURL": (
                    "https://abc.kinesisvideo.us-west-2.amazonaws.com/"
                    f"getHLSMasterPlaylist.m3u8?SessionToken={self.secret}"
                )
            }

        self.archived.get_hls_streaming_session_url = session
        self.module["_archived_media_client"] = lambda *_args: self.archived

    def invoke(self, value, *, browser=False):
        response = self.module["handler"](
            {
                "rawPath": (
                    "/video/browser-session/ch1" if browser else "/video/session/ch1"
                ),
                "queryStringParameters": {"max_fragments": value},
                "requestContext": {
                    "domainName": "api.example.test",
                    "stage": "$default",
                },
            },
            None,
        )
        return response, json.loads(response["body"])

    def test_perception_can_request_two_fragment_live_edge(self):
        response, body = self.invoke("2")
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["maxMediaPlaylistFragmentResults"], 2)
        self.assertEqual(body["delivery"], "DIRECT_KINESIS")
        self.assertIn(self.secret, body["hlsUrl"])
        self.assertEqual(len(self.s3.put_calls), 0)
        self.assertEqual(
            self.archived.calls[-1]["MaxMediaPlaylistFragmentResults"], 2
        )

    def test_browser_gets_opaque_same_origin_proxy(self):
        response, body = self.invoke("2", browser=True)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["maxMediaPlaylistFragmentResults"], 2)
        self.assertEqual(body["delivery"], "SAME_ORIGIN_PROXY")
        self.assertTrue(body["hlsUrl"].startswith("https://api.example.test/video/proxy/"))
        self.assertNotIn(self.secret, response["body"])
        self.assertEqual(len(self.s3.put_calls), 1)

    def test_live_fragment_count_is_bounded(self):
        response, body = self.invoke("1")
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(body["error"], "invalid_max_fragments")
        self.assertEqual(self.archived.calls, [])


class HlsProxyTest(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.module = load_generated_lambda(FakeTable(), self.s3)
        self.secret = "v" * 64
        self.archived = types.SimpleNamespace()
        self.archived.get_hls_streaming_session_url = lambda **_kwargs: {
            "HLSStreamingSessionURL": (
                "https://abc.kinesisvideo.us-west-2.amazonaws.com/"
                f"getHLSMasterPlaylist.m3u8?SessionToken={self.secret}"
            )
        }
        self.module["_archived_media_client"] = lambda *_args: self.archived
        self.context = {
            "domainName": "api.example.test",
            "stage": "$default",
        }

    def new_session(self):
        response = self.module["handler"](
            {
                "rawPath": "/video/browser-session/ch4",
                "queryStringParameters": {
                    "start": "2026-07-10T05:00:00Z",
                    "end": "2026-07-10T05:15:00Z",
                },
                "requestContext": self.context,
            },
            None,
        )
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["delivery"], "SAME_ORIGIN_PROXY")
        self.assertNotIn(self.secret, response["body"])
        return body["hlsUrl"]

    def invoke_proxy(self, url):
        path = url.split("api.example.test", 1)[-1]
        return self.module["handler"](
            {"rawPath": path, "requestContext": self.context}, None
        )

    @staticmethod
    def decoded(response):
        return base64.b64decode(response["body"])

    @staticmethod
    def first_proxy_url(playlist, marker="/video/proxy/"):
        for line in playlist.decode("utf-8").splitlines():
            if marker in line:
                match = re.search(r'https://[^" ]+/video/proxy/[^" ]+', line)
                if match:
                    return match.group(0)
        raise AssertionError("rewritten proxy URL not found")

    def test_recursively_rewrites_playlists_and_proxies_binary_without_secret(self):
        master_url = self.new_session()
        master = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\n"
            "getHLSMediaPlaylist.m3u8?"
            f"SessionToken={self.secret}&Track=1\n"
        ).encode("utf-8")
        media = (
            "#EXTM3U\n#EXT-X-MAP:URI=\"getMP4InitFragment.mp4?"
            f"SessionToken={self.secret}&FragmentNumber=init\"\n"
            "#EXTINF:2.0,\ngetMP4MediaFragment.mp4?"
            f"SessionToken={self.secret}&FragmentNumber=123\n"
        ).encode("utf-8")
        calls = []

        def fetch(url, kind):
            calls.append((url, kind))
            if "getHLSMasterPlaylist" in url:
                return master
            if "getHLSMediaPlaylist" in url:
                return media
            return b"fragment-bytes"

        self.module["_fetch_hls_upstream"] = fetch
        master_response = self.invoke_proxy(master_url)
        self.assertEqual(master_response["statusCode"], 200)
        self.assertEqual(
            master_response["headers"]["content-type"],
            "application/vnd.apple.mpegurl; charset=utf-8",
        )
        self.assertEqual(master_response["headers"]["access-control-allow-origin"], "*")
        rewritten_master = self.decoded(master_response)
        self.assertNotIn(self.secret.encode(), rewritten_master)

        media_url = self.first_proxy_url(rewritten_master)
        media_response = self.invoke_proxy(media_url)
        self.assertEqual(media_response["statusCode"], 200)
        rewritten_media = self.decoded(media_response)
        self.assertNotIn(self.secret.encode(), rewritten_media)

        segment_url = self.first_proxy_url(rewritten_media)
        segment_response = self.invoke_proxy(segment_url)
        self.assertEqual(segment_response["statusCode"], 200)
        self.assertEqual(segment_response["headers"]["content-type"], "video/mp4")
        self.assertEqual(self.decoded(segment_response), b"fragment-bytes")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(self.secret in url for url, _kind in calls))

    def test_tampered_resource_is_rejected_before_upstream_fetch(self):
        master_url = self.new_session()
        master = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
            f"getHLSMediaPlaylist.m3u8?SessionToken={self.secret}\n"
        ).encode("utf-8")
        self.module["_fetch_hls_upstream"] = lambda _url, _kind: master
        media_url = self.first_proxy_url(self.decoded(self.invoke_proxy(master_url)))
        tampered = media_url[:-1] + ("0" if media_url[-1] != "0" else "1")
        calls = []
        self.module["_fetch_hls_upstream"] = lambda *args: calls.append(args)
        response = self.invoke_proxy(tampered)
        self.assertEqual(response["statusCode"], 404)
        self.assertEqual(calls, [])
        self.assertNotIn(self.secret, response["body"])

    def test_cross_origin_playlist_child_fails_closed_without_disclosure(self):
        master_url = self.new_session()
        self.module["_fetch_hls_upstream"] = lambda _url, _kind: (
            b"#EXTM3U\nhttps://evil.example/getHLSMediaPlaylist.m3u8\n"
        )
        response = self.invoke_proxy(master_url)
        self.assertEqual(response["statusCode"], 502)
        self.assertEqual(json.loads(response["body"])["error"], "hls_upstream_origin_invalid")
        self.assertNotIn("evil.example", response["body"])
        self.assertNotIn(self.secret, response["body"])

    def test_expired_server_side_session_is_deleted_and_returns_gone(self):
        master_url = self.new_session()
        state_key = next(iter(self.s3.objects))
        state = json.loads(self.s3.objects[state_key])
        state["expiresAtEpoch"] = 0
        self.s3.objects[state_key] = json.dumps(state).encode("utf-8")
        response = self.invoke_proxy(master_url)
        self.assertEqual(response["statusCode"], 410)
        self.assertEqual(json.loads(response["body"])["error"], "hls_proxy_session_expired")
        self.assertNotIn(state_key, self.s3.objects)

    def test_boolean_schema_version_cannot_spoof_proxy_state(self):
        master_url = self.new_session()
        state_key = next(iter(self.s3.objects))
        state = json.loads(self.s3.objects[state_key])
        state["schemaVersion"] = True
        self.s3.objects[state_key] = json.dumps(state).encode("utf-8")
        response = self.invoke_proxy(master_url)
        self.assertEqual(response["statusCode"], 502)
        self.assertEqual(json.loads(response["body"])["error"], "hls_proxy_state_invalid")

    def test_upstream_content_length_bound_fails_before_body_read(self):
        class OversizedResponse:
            status = 200
            headers = {
                "content-length": str(4 * 1024 * 1024 + 1),
                "content-encoding": "identity",
            }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

            def read(self, _size):
                raise AssertionError("oversized body must not be read")

        self.module["HLS_PROXY_OPENER"] = types.SimpleNamespace(
            open=lambda *_args, **_kwargs: OversizedResponse()
        )
        with self.assertRaises(self.module["HlsProxyError"]) as raised:
            self.module["_fetch_hls_upstream"](
                "https://abc.kinesisvideo.us-west-2.amazonaws.com/"
                f"getMP4MediaFragment.mp4?SessionToken={self.secret}",
                "mp4",
            )
        self.assertEqual(raised.exception.code, "hls_upstream_response_too_large")

    def test_redirect_handler_never_forwards_signed_request(self):
        redirect = self.module["_RejectRedirects"]()
        self.assertIsNone(
            redirect.redirect_request(None, None, 302, "Found", {}, "https://evil.example")
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
