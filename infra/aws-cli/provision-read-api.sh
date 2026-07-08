#!/usr/bin/env bash
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing dependency: $1" >&2
    exit 1
  }
}

need aws
need jq
need zip

AWS_REGION="${AWS_REGION:-us-west-1}"
TABLE_NAME="${TABLE_NAME:-v2x-backend-detections}"
INGEST_LAMBDA_NAME="${INGEST_LAMBDA_NAME:-v2x-backend-ingest}"
READ_LAMBDA_NAME="${READ_LAMBDA_NAME:-v2x-backend-read}"
API_NAME="${API_NAME:-v2x-backend-api}"
STAGE_NAME="${STAGE_NAME:-\$default}"
ATTACH_DDB_READ_POLICY="${ATTACH_DDB_READ_POLICY:-true}"
READ_POLICY_NAME="${READ_POLICY_NAME:-v2x-backend-detections-ddb-read}"
VIDEO_AWS_REGION="${VIDEO_AWS_REGION:-us-west-2}"
VIDEO_STREAM_PREFIX="${VIDEO_STREAM_PREFIX:-v2x-backend-cam-}"
VIDEO_HLS_EXPIRES_SECONDS="${VIDEO_HLS_EXPIRES_SECONDS:-300}"
VIDEO_ONDEMAND_EXPIRES_SECONDS="${VIDEO_ONDEMAND_EXPIRES_SECONDS:-3600}"
SITE_GEOHASH="${SITE_GEOHASH:-9q9p8}"
SNAPSHOT_URL_EXPIRES_SECONDS="${SNAPSHOT_URL_EXPIRES_SECONDS:-300}"
DEMO_VIDEOS_PREFIX="${DEMO_VIDEOS_PREFIX:-demo-videos/}"
DEMO_VIDEO_URL_EXPIRES_SECONDS="${DEMO_VIDEO_URL_EXPIRES_SECONDS:-3600}"

export AWS_REGION

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
STATE_BUCKET="${STATE_BUCKET:-v2x-backend-state-${ACCOUNT_ID}-${AWS_REGION}}"

echo "Region: ${AWS_REGION}"
echo "Account: ${ACCOUNT_ID}"

ROLE_ARN="$(aws lambda get-function --function-name "${INGEST_LAMBDA_NAME}" --query Configuration.Role --output text)"
ROLE_NAME="${ROLE_ARN##*/}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_DIR="${HERE}/.secrets"
mkdir -p "${SECRETS_DIR}"

if [[ "${ATTACH_DDB_READ_POLICY}" == "true" ]]; then
  cat > "${SECRETS_DIR}/lambda-ddb-read-inline.json" <<JSON
{
  "Version":"2012-10-17",
  "Statement":[
    {
      "Effect":"Allow",
      "Action":[ "dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan" ],
      "Resource":[
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${TABLE_NAME}",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${TABLE_NAME}/index/*"
      ]
    },
    {
      "Effect":"Allow",
      "Action":[
        "kinesisvideo:DescribeStream",
        "kinesisvideo:GetDataEndpoint",
        "kinesisvideo:ListFragments"
      ],
      "Resource":[
        "arn:aws:kinesisvideo:${VIDEO_AWS_REGION}:${ACCOUNT_ID}:stream/${VIDEO_STREAM_PREFIX}*"
      ]
    },
    {
      "Effect":"Allow",
      "Action":[
        "kinesisvideo:GetHLSStreamingSessionURL"
      ],
      "Resource":"*"
    },
    {
      "Effect":"Allow",
      "Action":[ "s3:ListBucket" ],
      "Resource":[ "arn:aws:s3:::${STATE_BUCKET}" ]
    },
    {
      "Effect":"Allow",
      "Action":[ "s3:GetObject" ],
      "Resource":[
        "arn:aws:s3:::${STATE_BUCKET}/api/*",
        "arn:aws:s3:::${STATE_BUCKET}/snapshots/*",
        "arn:aws:s3:::${STATE_BUCKET}/${DEMO_VIDEOS_PREFIX}*"
      ]
    }
  ]
}
JSON
  aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name "${READ_POLICY_NAME}" \
    --policy-document "file://${SECRETS_DIR}/lambda-ddb-read-inline.json" >/dev/null || true
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

cat > "${WORKDIR}/index.py" <<PY
import base64
import json
import mimetypes
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote
from botocore.config import Config as BotoConfig

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

TABLE_NAME = os.environ.get("TABLE_NAME", "${TABLE_NAME}")
GSI_NAME = os.environ.get("GSI_NAME", "gsi_geohash_time")
MAX_LIMIT = int(os.environ.get("MAX_LIMIT", "200"))
VIDEO_AWS_REGION = os.environ.get("VIDEO_AWS_REGION", "${VIDEO_AWS_REGION}")
VIDEO_STREAM_PREFIX = os.environ.get("VIDEO_STREAM_PREFIX", "${VIDEO_STREAM_PREFIX}")
VIDEO_HLS_EXPIRES_SECONDS = int(os.environ.get("VIDEO_HLS_EXPIRES_SECONDS", "${VIDEO_HLS_EXPIRES_SECONDS}"))
VIDEO_ONDEMAND_EXPIRES_SECONDS = int(os.environ.get("VIDEO_ONDEMAND_EXPIRES_SECONDS", "${VIDEO_ONDEMAND_EXPIRES_SECONDS}"))
SITE_GEOHASH = os.environ.get("SITE_GEOHASH", "${SITE_GEOHASH}")
STATE_BUCKET = os.environ.get("STATE_BUCKET", "${STATE_BUCKET}")
SNAPSHOT_URL_EXPIRES_SECONDS = int(os.environ.get("SNAPSHOT_URL_EXPIRES_SECONDS", "${SNAPSHOT_URL_EXPIRES_SECONDS}"))
DEMO_VIDEOS_PREFIX = os.environ.get("DEMO_VIDEOS_PREFIX", "${DEMO_VIDEOS_PREFIX}")
DEMO_VIDEO_URL_EXPIRES_SECONDS = int(os.environ.get("DEMO_VIDEO_URL_EXPIRES_SECONDS", "${DEMO_VIDEO_URL_EXPIRES_SECONDS}"))

ddb = boto3.resource("dynamodb")
table = ddb.Table(TABLE_NAME)
video_client = boto3.client("kinesisvideo", region_name=VIDEO_AWS_REGION, config=BotoConfig(retries={"max_attempts": 3}))
s3_client = boto3.client("s3")

ALLOWED_CAMERA_IDS = {"ch1", "ch2", "ch3", "ch4"}
ALLOWED_DEMO_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}

def _jsonable(value):
    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value

def _strip_api_fields(item):
    # Keep storage as-is, but remove fleet identifiers from the public read API.
    if not isinstance(item, dict):
        return item
    item = dict(item)
    item.pop("fleet_id", None)
    return item

def _b64(obj):
    if obj is None:
        return None
    return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("utf-8")

def _unb64(s):
    if not s:
        return None
    return json.loads(base64.urlsafe_b64decode(s.encode("utf-8")).decode("utf-8"))

def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
        },
        "body": json.dumps(body),
    }

def _api_base_url(event):
    headers = event.get("headers") or {}
    request_context = event.get("requestContext") or {}
    proto = headers.get("x-forwarded-proto", "https")
    domain_name = request_context.get("domainName") or headers.get("host", "")
    stage = request_context.get("stage") or ""

    if stage and stage != ("$" + "default"):
        return f"{proto}://{domain_name}/{stage}"
    return f"{proto}://{domain_name}"

def _get_s3_json(key):
    try:
        response = s3_client.get_object(Bucket=STATE_BUCKET, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "ClientError")
        status = 404 if error_code in {"NoSuchKey", "NoSuchBucket", "404", "NotFound"} else 502
        return None, _resp(status, {"error": "state_asset_unavailable", "detail": error_code, "key": key})

    body = response["Body"].read().decode("utf-8")
    try:
        return json.loads(body), None
    except json.JSONDecodeError:
        return None, _resp(502, {"error": "state_asset_invalid_json", "key": key})

def _snapshot_api_url(event, object_id, snapshot_timestamp):
    base_url = _api_base_url(event)
    encoded_object_id = quote(str(object_id), safe="")
    if snapshot_timestamp:
        encoded_version = quote(str(snapshot_timestamp), safe="")
        return f"{base_url}/snapshots/{encoded_object_id}/latest?v={encoded_version}"
    return f"{base_url}/snapshots/{encoded_object_id}/latest"

def _get_state(event):
    payload, error = _get_s3_json("api/state.json")
    if error:
        return error

    objects = []
    for item in payload.get("objects", []) or []:
        obj = dict(item)
        if obj.get("snapshot_url") and obj.get("object_id"):
            obj["snapshot_url"] = _snapshot_api_url(
                event,
                obj["object_id"],
                obj.get("snapshot_timestamp"),
            )
        objects.append(obj)
    payload["objects"] = objects
    return _resp(200, payload)

def _get_map_data():
    payload, error = _get_s3_json("api/map-data.json")
    if error:
        return error
    return _resp(200, payload)

def _get_drive_config():
    payload, error = _get_s3_json("api/drive-config.json")
    if error:
        return error
    return _resp(200, payload)

def _get_snapshot(object_id):
    key = f"snapshots/{object_id}/latest.jpg"
    try:
        s3_client.head_object(Bucket=STATE_BUCKET, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "ClientError")
        status = 404 if error_code in {"NoSuchKey", "404", "NotFound"} else 502
        return _resp(
            status,
            {
                "error": "snapshot_unavailable",
                "objectId": object_id,
                "detail": error_code,
            },
        )

    signed_url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": STATE_BUCKET, "Key": key},
        ExpiresIn=SNAPSHOT_URL_EXPIRES_SECONDS,
    )
    return {
        "statusCode": 307,
        "headers": {
            "location": signed_url,
            "cache-control": "no-store",
            "access-control-allow-origin": "*",
        },
        "body": "",
    }

def _demo_video_title(filename):
    stem, _sep, _ext = filename.rpartition(".")
    source = stem or filename
    parts = source.replace("_", " ").replace("-", " ").split()
    return " ".join(parts) if parts else filename

def _get_demo_videos():
    paginator = s3_client.get_paginator("list_objects_v2")
    items = []

    for page in paginator.paginate(Bucket=STATE_BUCKET, Prefix=DEMO_VIDEOS_PREFIX):
        for obj in page.get("Contents", []) or []:
            key = obj.get("Key", "")
            if not key or key.endswith("/"):
                continue

            filename = key.rsplit("/", 1)[-1]
            lower_name = filename.lower()
            if not any(lower_name.endswith(ext) for ext in ALLOWED_DEMO_VIDEO_EXTENSIONS):
                continue

            signed_url = s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": STATE_BUCKET, "Key": key},
                ExpiresIn=DEMO_VIDEO_URL_EXPIRES_SECONDS,
            )
            content_type = mimetypes.guess_type(filename)[0] or "video/mp4"
            last_modified = obj.get("LastModified")
            items.append(
                {
                    "key": key,
                    "fileName": filename,
                    "title": _demo_video_title(filename),
                    "url": signed_url,
                    "sizeBytes": obj.get("Size", 0),
                    "lastModified": last_modified.isoformat() if last_modified else None,
                    "contentType": content_type,
                }
            )

    items.sort(key=lambda item: item.get("lastModified") or "", reverse=True)
    return _resp(200, {"items": items})

def _camera_stream_name(camera_id):
    return f"{VIDEO_STREAM_PREFIX}{camera_id}"

def _parse_ts(value):
    """Parse an ISO-8601 timestamp (with optional trailing Z) to aware UTC."""
    if not value:
        return None
    v = str(value).strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _iso_millis(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def _ts_event_bounds(start_dt, end_dt):
    # ts_event is "{timestamp_utc}#{event_id}" with millisecond timestamps.
    # Normalising both bounds to millisecond precision keeps the lexicographic
    # BETWEEN correct; "~" sorts after both "Z" and "#".
    return _iso_millis(start_dt), _iso_millis(end_dt) + "~"

def _resolve_window(qs, default_hours=24, max_hours=48):
    start_dt = _parse_ts(qs.get("start"))
    end_dt = _parse_ts(qs.get("end"))
    if end_dt is None:
        end_dt = datetime.now(timezone.utc)
    if start_dt is None:
        start_dt = end_dt - timedelta(hours=default_hours)
    if start_dt >= end_dt:
        return None, None, _resp(400, {"error": "invalid_range", "detail": "start must be before end"})
    if end_dt - start_dt > timedelta(hours=max_hours):
        start_dt = end_dt - timedelta(hours=max_hours)
    return start_dt, end_dt, None

def _archived_media_client(stream_name, api_name):
    endpoint = video_client.get_data_endpoint(
        StreamName=stream_name,
        APIName=api_name,
    )["DataEndpoint"]
    return boto3.client(
        "kinesis-video-archived-media",
        region_name=VIDEO_AWS_REGION,
        endpoint_url=endpoint,
        config=BotoConfig(retries={"max_attempts": 3}),
    )

def _get_hls_session(camera_id, qs):
    if camera_id not in ALLOWED_CAMERA_IDS:
        return _resp(404, {"error": "camera_not_found", "cameraId": camera_id})

    stream_name = _camera_stream_name(camera_id)
    start_dt = _parse_ts(qs.get("start"))
    end_dt = _parse_ts(qs.get("end"))
    on_demand = start_dt is not None or end_dt is not None

    if on_demand:
        if start_dt is None or end_dt is None:
            return _resp(400, {"error": "invalid_range", "detail": "archive playback requires both start and end"})
        if start_dt >= end_dt:
            return _resp(400, {"error": "invalid_range", "detail": "start must be before end"})
        if end_dt - start_dt > timedelta(hours=24):
            return _resp(400, {"error": "invalid_range", "detail": "window must be 24 hours or less"})

    try:
        archived_media = _archived_media_client(stream_name, "GET_HLS_STREAMING_SESSION_URL")
        if on_demand:
            hls_url = archived_media.get_hls_streaming_session_url(
                StreamName=stream_name,
                PlaybackMode="ON_DEMAND",
                HLSFragmentSelector={
                    "FragmentSelectorType": "SERVER_TIMESTAMP",
                    "TimestampRange": {
                        "StartTimestamp": start_dt,
                        "EndTimestamp": end_dt,
                    },
                },
                Expires=VIDEO_ONDEMAND_EXPIRES_SECONDS,
                ContainerFormat="FRAGMENTED_MP4",
                DiscontinuityMode="ON_DISCONTINUITY",
                DisplayFragmentTimestamp="ALWAYS",
                MaxMediaPlaylistFragmentResults=5000,
            )["HLSStreamingSessionURL"]
            return _resp(
                200,
                {
                    "cameraId": camera_id,
                    "streamName": stream_name,
                    "playbackMode": "ON_DEMAND",
                    "hlsUrl": hls_url,
                    "expiresIn": VIDEO_ONDEMAND_EXPIRES_SECONDS,
                    "start": _iso_millis(start_dt),
                    "end": _iso_millis(end_dt),
                    "region": VIDEO_AWS_REGION,
                },
            )
        hls_url = archived_media.get_hls_streaming_session_url(
            StreamName=stream_name,
            PlaybackMode="LIVE",
            Expires=VIDEO_HLS_EXPIRES_SECONDS,
            ContainerFormat="FRAGMENTED_MP4",
            DiscontinuityMode="ALWAYS",
            DisplayFragmentTimestamp="ALWAYS",
            MaxMediaPlaylistFragmentResults=5,
        )["HLSStreamingSessionURL"]
        return _resp(
            200,
            {
                "cameraId": camera_id,
                "streamName": stream_name,
                "playbackMode": "LIVE",
                "hlsUrl": hls_url,
                "expiresIn": VIDEO_HLS_EXPIRES_SECONDS,
                "region": VIDEO_AWS_REGION,
            },
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "ClientError")
        status = 404 if error_code in {"ResourceNotFoundException", "NoDataRetentionException"} else 502
        return _resp(
            status,
            {
                "error": "video_session_unavailable",
                "cameraId": camera_id,
                "streamName": stream_name,
                "detail": error_code,
            },
        )

def _get_video_coverage(camera_id, qs):
    """Merged fragment intervals so the timeline UI can grey out gaps."""
    if camera_id not in ALLOWED_CAMERA_IDS:
        return _resp(404, {"error": "camera_not_found", "cameraId": camera_id})

    stream_name = _camera_stream_name(camera_id)
    start_dt, end_dt, err = _resolve_window(qs)
    if err:
        return err

    try:
        archived_media = _archived_media_client(stream_name, "LIST_FRAGMENTS")
        fragments = []
        next_token = None
        pages = 0
        # ~2s fragments -> a 24h window is ~45 pages of 1000, which cannot
        # finish inside API Gateway's 30s integration limit. Stop on a time
        # budget and report truncation; the web client requests coverage in
        # ~4h chunks so real queries never hit this.
        deadline = time.monotonic() + 20.0
        while pages < 60 and time.monotonic() < deadline:
            kwargs = {
                "StreamName": stream_name,
                "MaxResults": 1000,
                "FragmentSelector": {
                    "FragmentSelectorType": "SERVER_TIMESTAMP",
                    "TimestampRange": {
                        "StartTimestamp": start_dt,
                        "EndTimestamp": end_dt,
                    },
                },
            }
            if next_token:
                kwargs["NextToken"] = next_token
            resp = archived_media.list_fragments(**kwargs)
            fragments.extend(resp.get("Fragments", []) or [])
            next_token = resp.get("NextToken")
            pages += 1
            if not next_token:
                break
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "ClientError")
        status = 404 if error_code in {"ResourceNotFoundException", "NoDataRetentionException"} else 502
        return _resp(status, {"error": "video_coverage_unavailable", "cameraId": camera_id, "detail": error_code})

    spans = sorted(
        (
            (
                f["ServerTimestamp"],
                f["ServerTimestamp"] + timedelta(milliseconds=int(f.get("FragmentLengthInMilliseconds") or 0)),
            )
            for f in fragments
            if f.get("ServerTimestamp") is not None
        ),
        key=lambda pair: pair[0],
    )

    gap_tolerance = timedelta(seconds=15)
    intervals = []
    for span_start, span_end in spans:
        if intervals and span_start - intervals[-1][1] <= gap_tolerance:
            if span_end > intervals[-1][1]:
                intervals[-1][1] = span_end
        else:
            intervals.append([span_start, span_end])

    return _resp(
        200,
        {
            "cameraId": camera_id,
            "start": _iso_millis(start_dt),
            "end": _iso_millis(end_dt),
            "intervals": [
                {"start": _iso_millis(s), "end": _iso_millis(e)} for s, e in intervals
            ],
            "fragmentCount": len(fragments),
            "truncated": next_token is not None,
        },
    )

def _range_filter_expression(qs):
    filters = []
    device_id = (qs.get("device_id") or "").strip()
    object_type = (qs.get("object_type") or "").strip()
    if device_id:
        filters.append(Attr("device_id").eq(device_id))
    if object_type:
        filters.append(Attr("object_type").eq(object_type))
    if not filters:
        return None
    condition = filters[0]
    for extra in filters[1:]:
        condition = condition & extra
    return condition

def _get_detections_range(qs, limit, exclusive_start_key):
    # All detections at the site share one precision-5 geohash, so the
    # geohash+ts_event GSI doubles as a time index.
    start_dt, end_dt, err = _resolve_window(qs)
    if err:
        return err

    start_key, end_key = _ts_event_bounds(start_dt, end_dt)
    kwargs = {
        "IndexName": GSI_NAME,
        "KeyConditionExpression": Key("geohash").eq(SITE_GEOHASH)
        & Key("ts_event").between(start_key, end_key),
        "Limit": limit,
        "ScanIndexForward": False,
    }
    condition = _range_filter_expression(qs)
    if condition is not None:
        kwargs["FilterExpression"] = condition
    if exclusive_start_key:
        kwargs["ExclusiveStartKey"] = exclusive_start_key
    resp = table.query(**kwargs)
    items = [_strip_api_fields(x) for x in (resp.get("Items", []) or [])]
    return _resp(
        200,
        {
            "items": _jsonable(items),
            "next": _b64(resp.get("LastEvaluatedKey")),
            "start": _iso_millis(start_dt),
            "end": _iso_millis(end_dt),
        },
    )

TIMELINE_MAX_PAGES = int(os.environ.get("TIMELINE_MAX_PAGES", "40"))

def _get_detections_timeline(qs):
    """Aggregate a time window into track events + a per-bucket histogram.

    Grouping happens here so the browser never has to page through tens of
    thousands of raw detection rows to draw timeline markers.
    """
    start_dt, end_dt, err = _resolve_window(qs)
    if err:
        return err

    try:
        bucket_seconds = int(qs.get("bucket") or "60")
    except ValueError:
        bucket_seconds = 60
    bucket_seconds = max(10, min(3600, bucket_seconds))

    start_key, end_key = _ts_event_bounds(start_dt, end_dt)
    base_kwargs = {
        "IndexName": GSI_NAME,
        "KeyConditionExpression": Key("geohash").eq(SITE_GEOHASH)
        & Key("ts_event").between(start_key, end_key),
        "ScanIndexForward": True,
        "ProjectionExpression": "object_id, object_type, timestamp_utc, device_id, confidence_score",
    }
    condition = _range_filter_expression(qs)
    if condition is not None:
        base_kwargs["FilterExpression"] = condition

    tracks = {}
    buckets = {}
    total = 0
    truncated = False
    exclusive_start_key = None
    for _ in range(TIMELINE_MAX_PAGES):
        kwargs = dict(base_kwargs)
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        resp = table.query(**kwargs)
        for item in resp.get("Items", []) or []:
            ts = _parse_ts(item.get("timestamp_utc"))
            if ts is None:
                continue
            total += 1
            object_id = str(item.get("object_id") or "unknown")
            object_type = str(item.get("object_type") or "unknown")
            confidence = item.get("confidence_score")
            confidence = float(confidence) if isinstance(confidence, (int, float, Decimal)) else 0.0

            track = tracks.get(object_id)
            if track is None:
                tracks[object_id] = {
                    "object_id": object_id,
                    "object_type": object_type,
                    "device_id": str(item.get("device_id") or ""),
                    "first_seen": ts,
                    "last_seen": ts,
                    "count": 1,
                    "max_confidence": confidence,
                }
            else:
                track["count"] += 1
                if ts < track["first_seen"]:
                    track["first_seen"] = ts
                if ts > track["last_seen"]:
                    track["last_seen"] = ts
                if confidence > track["max_confidence"]:
                    track["max_confidence"] = confidence

            bucket_idx = int((ts - start_dt).total_seconds() // bucket_seconds)
            counts = buckets.setdefault(bucket_idx, {})
            counts[object_type] = counts.get(object_type, 0) + 1

        exclusive_start_key = resp.get("LastEvaluatedKey")
        if not exclusive_start_key:
            break
    else:
        truncated = True

    events = sorted(tracks.values(), key=lambda t: t["first_seen"])
    return _resp(
        200,
        {
            "start": _iso_millis(start_dt),
            "end": _iso_millis(end_dt),
            "bucketSeconds": bucket_seconds,
            "totalDetections": total,
            "truncated": truncated,
            "events": [
                {
                    "object_id": t["object_id"],
                    "object_type": t["object_type"],
                    "device_id": t["device_id"],
                    "first_seen": _iso_millis(t["first_seen"]),
                    "last_seen": _iso_millis(t["last_seen"]),
                    "count": t["count"],
                    "max_confidence": round(t["max_confidence"], 4),
                }
                for t in events
            ],
            "histogram": [
                {
                    "bucket_start": _iso_millis(start_dt + timedelta(seconds=idx * bucket_seconds)),
                    "counts": buckets[idx],
                }
                for idx in sorted(buckets)
            ],
        },
    )

def handler(event, context):
    path = (event.get("rawPath") or event.get("path") or "").rstrip("/")
    qs = event.get("queryStringParameters") or {}
    path_params = event.get("pathParameters") or {}

    try:
        limit = int(qs.get("limit") or "50")
    except ValueError:
        limit = 50
    limit = max(1, min(MAX_LIMIT, limit))

    next_token = qs.get("next")
    exclusive_start_key = _unb64(next_token)

    if path.startswith("/video/session/"):
        camera_id = path_params.get("camera_id") or path.split("/video/session/", 1)[1]
        return _get_hls_session(camera_id, qs)

    if path.startswith("/video/coverage/"):
        camera_id = path_params.get("camera_id") or path.split("/video/coverage/", 1)[1]
        return _get_video_coverage(camera_id, qs)

    if path == "/detections/timeline":
        return _get_detections_timeline(qs)

    if path == "/demo-videos":
        return _get_demo_videos()

    if path == "/state":
        return _get_state(event)

    if path == "/map-data":
        return _get_map_data()

    if path == "/drive-config":
        return _get_drive_config()

    if path.startswith("/snapshots/") and path.endswith("/latest"):
        object_id = path_params.get("object_id") or path.split("/snapshots/", 1)[1].rsplit("/latest", 1)[0]
        return _get_snapshot(object_id)

    if path.startswith("/detections/object/"):
        object_id = path_params.get("object_id") or path.split("/detections/object/", 1)[1]
        kwargs = {
            "KeyConditionExpression": Key("object_id").eq(object_id),
            "Limit": limit,
            "ScanIndexForward": False,
        }
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        resp = table.query(**kwargs)
        items = [_strip_api_fields(x) for x in (resp.get("Items", []) or [])]
        return _resp(
            200,
            {
                "items": _jsonable(items),
                "next": _b64(resp.get("LastEvaluatedKey")),
            },
        )

    if path.startswith("/detections/geohash/"):
        geohash = path_params.get("geohash") or path.split("/detections/geohash/", 1)[1]
        kwargs = {
            "IndexName": GSI_NAME,
            "KeyConditionExpression": Key("geohash").eq(geohash),
            "Limit": limit,
            "ScanIndexForward": False,
        }
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        resp = table.query(**kwargs)
        items = [_strip_api_fields(x) for x in (resp.get("Items", []) or [])]
        return _resp(
            200,
            {
                "items": _jsonable(items),
                "next": _b64(resp.get("LastEvaluatedKey")),
            },
        )

    if path == "/detections/range":
        return _get_detections_range(qs, limit, exclusive_start_key)

    if path == "/detections/recent":
        # NOTE: DynamoDB has no global "recent" query without a dedicated index.
        # This is a best-effort scan for small tables; sort client-side.
        kwargs = {"Limit": limit}
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        resp = table.scan(**kwargs)
        items = [_strip_api_fields(x) for x in (resp.get("Items", []) or [])]
        items = sorted(items, key=lambda x: (x.get("timestamp_utc") or ""), reverse=True)
        return _resp(
            200,
            {
                "items": _jsonable(items),
                "next": _b64(resp.get("LastEvaluatedKey")),
            },
        )

    if path in ("", "/"):
        return _resp(
            200,
            {
                "ok": True,
                "routes": [
                    "/demo-videos",
                    "/state",
                    "/map-data",
                    "/drive-config",
                    "/snapshots/{object_id}/latest",
                    "/detections/range",
                    "/detections/recent",
                    "/detections/timeline",
                    "/detections/object/{object_id}",
                    "/detections/geohash/{geohash}",
                    "/video/session/{camera_id}",
                    "/video/coverage/{camera_id}",
                ],
            },
        )

    return _resp(404, {"error": "not_found", "path": path})
PY

(cd "${WORKDIR}" && zip -q function.zip index.py)

if ! aws lambda get-function --function-name "${READ_LAMBDA_NAME}" >/dev/null 2>&1; then
  aws lambda create-function \
    --function-name "${READ_LAMBDA_NAME}" \
    --runtime python3.12 \
    --handler index.handler \
    --role "${ROLE_ARN}" \
    --timeout 30 \
    --environment "Variables={TABLE_NAME=${TABLE_NAME},GSI_NAME=gsi_geohash_time,MAX_LIMIT=200,VIDEO_AWS_REGION=${VIDEO_AWS_REGION},VIDEO_STREAM_PREFIX=${VIDEO_STREAM_PREFIX},VIDEO_HLS_EXPIRES_SECONDS=${VIDEO_HLS_EXPIRES_SECONDS},STATE_BUCKET=${STATE_BUCKET},SNAPSHOT_URL_EXPIRES_SECONDS=${SNAPSHOT_URL_EXPIRES_SECONDS},DEMO_VIDEOS_PREFIX=${DEMO_VIDEOS_PREFIX},DEMO_VIDEO_URL_EXPIRES_SECONDS=${DEMO_VIDEO_URL_EXPIRES_SECONDS},VIDEO_ONDEMAND_EXPIRES_SECONDS=${VIDEO_ONDEMAND_EXPIRES_SECONDS},SITE_GEOHASH=${SITE_GEOHASH}}" \
    --zip-file "fileb://${WORKDIR}/function.zip" >/dev/null
else
  aws lambda update-function-code \
    --function-name "${READ_LAMBDA_NAME}" \
    --zip-file "fileb://${WORKDIR}/function.zip" >/dev/null

      aws lambda update-function-configuration \
        --function-name "${READ_LAMBDA_NAME}" \
        --timeout 30 \
        --environment "Variables={TABLE_NAME=${TABLE_NAME},GSI_NAME=gsi_geohash_time,MAX_LIMIT=200,VIDEO_AWS_REGION=${VIDEO_AWS_REGION},VIDEO_STREAM_PREFIX=${VIDEO_STREAM_PREFIX},VIDEO_HLS_EXPIRES_SECONDS=${VIDEO_HLS_EXPIRES_SECONDS},STATE_BUCKET=${STATE_BUCKET},SNAPSHOT_URL_EXPIRES_SECONDS=${SNAPSHOT_URL_EXPIRES_SECONDS},DEMO_VIDEOS_PREFIX=${DEMO_VIDEOS_PREFIX},DEMO_VIDEO_URL_EXPIRES_SECONDS=${DEMO_VIDEO_URL_EXPIRES_SECONDS},VIDEO_ONDEMAND_EXPIRES_SECONDS=${VIDEO_ONDEMAND_EXPIRES_SECONDS},SITE_GEOHASH=${SITE_GEOHASH}}" >/dev/null || {
      aws lambda wait function-updated --function-name "${READ_LAMBDA_NAME}"
      aws lambda update-function-configuration \
        --function-name "${READ_LAMBDA_NAME}" \
        --timeout 30 \
        --environment "Variables={TABLE_NAME=${TABLE_NAME},GSI_NAME=gsi_geohash_time,MAX_LIMIT=200,VIDEO_AWS_REGION=${VIDEO_AWS_REGION},VIDEO_STREAM_PREFIX=${VIDEO_STREAM_PREFIX},VIDEO_HLS_EXPIRES_SECONDS=${VIDEO_HLS_EXPIRES_SECONDS},STATE_BUCKET=${STATE_BUCKET},SNAPSHOT_URL_EXPIRES_SECONDS=${SNAPSHOT_URL_EXPIRES_SECONDS},DEMO_VIDEOS_PREFIX=${DEMO_VIDEOS_PREFIX},DEMO_VIDEO_URL_EXPIRES_SECONDS=${DEMO_VIDEO_URL_EXPIRES_SECONDS},VIDEO_ONDEMAND_EXPIRES_SECONDS=${VIDEO_ONDEMAND_EXPIRES_SECONDS},SITE_GEOHASH=${SITE_GEOHASH}}" >/dev/null
    }
fi

READ_LAMBDA_ARN="$(aws lambda get-function --function-name "${READ_LAMBDA_NAME}" --query Configuration.FunctionArn --output text)"

API_ID=""
EXISTING="$(aws apigatewayv2 get-apis --query 'Items[?Name==`'"${API_NAME}"'`].ApiId' --output text)"
if [[ -n "${EXISTING}" && "${EXISTING}" != "None" ]]; then
  API_ID="${EXISTING}"
else
  API_ID="$(aws apigatewayv2 create-api \
    --name "${API_NAME}" \
    --protocol-type HTTP \
    --cors-configuration AllowOrigins='*',AllowMethods='GET,OPTIONS',AllowHeaders='content-type' \
    --query ApiId --output text)"
fi

INTEGRATION_ID="$(aws apigatewayv2 create-integration \
  --api-id "${API_ID}" \
  --integration-type AWS_PROXY \
  --integration-uri "${READ_LAMBDA_ARN}" \
  --payload-format-version "2.0" \
  --query IntegrationId --output text)"

aws apigatewayv2 create-route --api-id "${API_ID}" --route-key "GET /detections/recent" --target "integrations/${INTEGRATION_ID}" >/dev/null || true
aws apigatewayv2 create-route --api-id "${API_ID}" --route-key "GET /detections/range" --target "integrations/${INTEGRATION_ID}" >/dev/null || true
aws apigatewayv2 create-route --api-id "${API_ID}" --route-key "GET /detections/object/{object_id}" --target "integrations/${INTEGRATION_ID}" >/dev/null || true
aws apigatewayv2 create-route --api-id "${API_ID}" --route-key "GET /detections/geohash/{geohash}" --target "integrations/${INTEGRATION_ID}" >/dev/null || true
aws apigatewayv2 create-route --api-id "${API_ID}" --route-key "GET /demo-videos" --target "integrations/${INTEGRATION_ID}" >/dev/null || true
aws apigatewayv2 create-route --api-id "${API_ID}" --route-key "GET /state" --target "integrations/${INTEGRATION_ID}" >/dev/null || true
aws apigatewayv2 create-route --api-id "${API_ID}" --route-key "GET /map-data" --target "integrations/${INTEGRATION_ID}" >/dev/null || true
aws apigatewayv2 create-route --api-id "${API_ID}" --route-key "GET /drive-config" --target "integrations/${INTEGRATION_ID}" >/dev/null || true
aws apigatewayv2 create-route --api-id "${API_ID}" --route-key "GET /snapshots/{object_id}/latest" --target "integrations/${INTEGRATION_ID}" >/dev/null || true
aws apigatewayv2 create-route --api-id "${API_ID}" --route-key "GET /video/session/{camera_id}" --target "integrations/${INTEGRATION_ID}" >/dev/null || true
aws apigatewayv2 create-route --api-id "${API_ID}" --route-key "GET /video/coverage/{camera_id}" --target "integrations/${INTEGRATION_ID}" >/dev/null || true
aws apigatewayv2 create-route --api-id "${API_ID}" --route-key "GET /detections/timeline" --target "integrations/${INTEGRATION_ID}" >/dev/null || true

if [[ "${STAGE_NAME}" == "\$default" ]]; then
  aws apigatewayv2 create-stage --api-id "${API_ID}" --stage-name "\$default" --auto-deploy >/dev/null || true
else
  aws apigatewayv2 create-stage --api-id "${API_ID}" --stage-name "${STAGE_NAME}" --auto-deploy >/dev/null || true
fi

STATEMENT_ID="apigw-${API_ID}"
if ! aws lambda get-policy --function-name "${READ_LAMBDA_NAME}" >/dev/null 2>&1 || \
   ! aws lambda get-policy --function-name "${READ_LAMBDA_NAME}" | jq -e --arg s "${STATEMENT_ID}" '.Policy|fromjson|.Statement[]|select(.Sid==$s)' >/dev/null 2>&1; then
  aws lambda add-permission \
    --function-name "${READ_LAMBDA_NAME}" \
    --statement-id "${STATEMENT_ID}" \
    --action "lambda:InvokeFunction" \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*/*/*" >/dev/null
fi

API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "${API_ID}" --query ApiEndpoint --output text)"

echo "Done."
echo "Read Lambda: ${READ_LAMBDA_NAME}"
echo "HTTP API: ${API_ENDPOINT}"
echo "Example:"
echo "  ${API_ENDPOINT}/detections/object/traffic_cone_001?limit=10"
