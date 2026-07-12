import base64
import hashlib
import hmac
import json
import math
import mimetypes
import os
import posixpath
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from botocore.config import Config as BotoConfig

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

TABLE_NAME = os.environ["TABLE_NAME"]
GSI_NAME = os.environ.get("GSI_NAME", "gsi_geohash_time")
MAX_LIMIT = int(os.environ.get("MAX_LIMIT", "200"))
VIDEO_AWS_REGION = os.environ["VIDEO_AWS_REGION"]
VIDEO_STREAM_PREFIX = os.environ["VIDEO_STREAM_PREFIX"]
VIDEO_HLS_EXPIRES_SECONDS = int(os.environ["VIDEO_HLS_EXPIRES_SECONDS"])
VIDEO_ONDEMAND_EXPIRES_SECONDS = int(os.environ["VIDEO_ONDEMAND_EXPIRES_SECONDS"])
HLS_PROXY_PREFIX = os.environ.get("HLS_PROXY_PREFIX", "hls-proxy/v1/").strip("/") + "/"
HLS_PROXY_FETCH_TIMEOUT_SECONDS = int(os.environ.get("HLS_PROXY_FETCH_TIMEOUT_SECONDS", "8"))
HLS_PROXY_PLAYLIST_MAX_BYTES = int(os.environ.get("HLS_PROXY_PLAYLIST_MAX_BYTES", "1048576"))
HLS_PROXY_SEGMENT_MAX_BYTES = int(os.environ.get("HLS_PROXY_SEGMENT_MAX_BYTES", "4194304"))
SITE_GEOHASH = os.environ["SITE_GEOHASH"]
STATE_BUCKET = os.environ["STATE_BUCKET"]
SNAPSHOT_URL_EXPIRES_SECONDS = int(os.environ["SNAPSHOT_URL_EXPIRES_SECONDS"])
DEMO_VIDEOS_PREFIX = os.environ["DEMO_VIDEOS_PREFIX"]
DEMO_VIDEO_URL_EXPIRES_SECONDS = int(os.environ["DEMO_VIDEO_URL_EXPIRES_SECONDS"])

ddb = boto3.resource("dynamodb")
table = ddb.Table(TABLE_NAME)
video_client = boto3.client("kinesisvideo", region_name=VIDEO_AWS_REGION, config=BotoConfig(retries={"max_attempts": 3}))
s3_client = boto3.client("s3")

ALLOWED_CAMERA_IDS = {"ch1", "ch2", "ch3", "ch4"}
ALLOWED_DEMO_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}
HLS_PROXY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
HLS_PROXY_RESOURCE_RE = re.compile(r"^(?:master|[A-Za-z0-9_-]{1,4096}\.[0-9a-f]{64})$")
HLS_URI_ATTRIBUTE_RE = re.compile(r'URI="([^"]+)"')
HLS_ALLOWED_BASENAMES = {
    "getHLSMasterPlaylist.m3u8": "playlist",
    "getHLSMediaPlaylist.m3u8": "playlist",
    "getMP4InitFragment.mp4": "mp4",
    "getMP4MediaFragment.mp4": "mp4",
    "getTSFragment": "ts",
}

if (
    not re.fullmatch(r"[A-Za-z0-9/_-]{1,128}/", HLS_PROXY_PREFIX)
    or ".." in HLS_PROXY_PREFIX
    or HLS_PROXY_PREFIX.startswith("/")
):
    raise RuntimeError("HLS proxy state prefix is invalid")
if not 60 <= VIDEO_HLS_EXPIRES_SECONDS <= 300:
    raise RuntimeError("live HLS session lifetime must be from 60 through 300 seconds")
if not 60 <= VIDEO_ONDEMAND_EXPIRES_SECONDS <= 3600:
    raise RuntimeError("archive HLS session lifetime must be from 60 through 3600 seconds")
if not 1 <= HLS_PROXY_FETCH_TIMEOUT_SECONDS <= 15:
    raise RuntimeError("HLS proxy timeout must be from 1 through 15 seconds")
if not 1024 <= HLS_PROXY_PLAYLIST_MAX_BYTES <= 1048576:
    raise RuntimeError("HLS proxy playlist bound is invalid")
if not 65536 <= HLS_PROXY_SEGMENT_MAX_BYTES <= 4194304:
    raise RuntimeError("HLS proxy segment bound is invalid")

class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

HLS_PROXY_OPENER = build_opener(_RejectRedirects())

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

class HlsProxyError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code

def _hls_proxy_error(error):
    return _resp(error.status, {"error": error.code})

def _hls_proxy_state_key(token):
    return f"{HLS_PROXY_PREFIX}{token}.json"

def _hls_proxy_signing_key(session):
    encoded = json.dumps(
        session["sessionQuery"], separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(b"v2x-hls-proxy-v1\0" + encoded).digest()

def _hls_proxy_kind(path):
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 1024:
        raise HlsProxyError(502, "hls_upstream_resource_invalid")
    decoded_path = unquote(path)
    if (
        decoded_path.startswith("//")
        or decoded_path != posixpath.normpath(decoded_path)
        or "\\" in decoded_path
        or any(ord(character) < 32 for character in decoded_path)
    ):
        raise HlsProxyError(502, "hls_upstream_resource_invalid")
    basename = posixpath.basename(decoded_path)
    kind = HLS_ALLOWED_BASENAMES.get(basename)
    if kind is None:
        raise HlsProxyError(502, "hls_upstream_resource_invalid")
    return kind

def _hls_proxy_origin(parsed):
    try:
        port = parsed.port
    except ValueError as exc:
        raise HlsProxyError(502, "hls_upstream_origin_invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise HlsProxyError(502, "hls_upstream_origin_invalid")
    hostname = parsed.hostname.lower()
    suffix = f".kinesisvideo.{VIDEO_AWS_REGION}.amazonaws.com"
    if not hostname.endswith(suffix) or hostname == suffix[1:]:
        raise HlsProxyError(502, "hls_upstream_origin_invalid")
    return f"https://{hostname}"

def _hls_proxy_base_url(event):
    request_context = event.get("requestContext") or {}
    domain = request_context.get("domainName") or ""
    if not isinstance(domain, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
        raise HlsProxyError(502, "hls_proxy_origin_unavailable")
    stage = request_context.get("stage") or ""
    stage_path = ""
    if stage and stage != ("$" + "default"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", stage):
            raise HlsProxyError(502, "hls_proxy_origin_unavailable")
        stage_path = "/" + stage
    return f"https://{domain}{stage_path}"

def _hls_proxy_url(event, token, resource_id):
    return (
        f"{_hls_proxy_base_url(event)}/video/proxy/"
        f"{quote(token, safe='')}/{quote(resource_id, safe='')}"
    )

def _store_hls_proxy_session(token, session):
    raw = json.dumps(session, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(raw) > 16384:
        raise HlsProxyError(502, "hls_proxy_state_invalid")
    try:
        s3_client.put_object(
            Bucket=STATE_BUCKET,
            Key=_hls_proxy_state_key(token),
            Body=raw,
            ContentType="application/json",
            CacheControl="no-store",
            ServerSideEncryption="AES256",
        )
    except ClientError as exc:
        raise HlsProxyError(502, "hls_proxy_state_unavailable") from exc

def _load_hls_proxy_session(token):
    if not HLS_PROXY_TOKEN_RE.fullmatch(token or ""):
        raise HlsProxyError(404, "hls_proxy_session_not_found")
    try:
        response = s3_client.get_object(
            Bucket=STATE_BUCKET,
            Key=_hls_proxy_state_key(token),
        )
        raw = response["Body"].read(16385)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        status = 404 if error_code in {"NoSuchKey", "404", "NotFound"} else 502
        code = "hls_proxy_session_not_found" if status == 404 else "hls_proxy_state_unavailable"
        raise HlsProxyError(status, code) from exc
    if len(raw) > 16384:
        raise HlsProxyError(502, "hls_proxy_state_invalid")
    try:
        session = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HlsProxyError(502, "hls_proxy_state_invalid") from exc
    if (
        not isinstance(session, dict)
        or type(session.get("schemaVersion")) is not int
        or session.get("schemaVersion") != 1
        or session.get("tokenHash") != hashlib.sha256(token.encode("ascii")).hexdigest()
        or session.get("cameraId") not in ALLOWED_CAMERA_IDS
        or isinstance(session.get("expiresAtEpoch"), bool)
        or not isinstance(session.get("expiresAtEpoch"), (int, float))
        or not math.isfinite(float(session.get("expiresAtEpoch")))
        or not isinstance(session.get("origin"), str)
        or not isinstance(session.get("masterPath"), str)
        or not isinstance(session.get("sessionQuery"), list)
    ):
        raise HlsProxyError(502, "hls_proxy_state_invalid")
    if time.time() >= float(session["expiresAtEpoch"]):
        try:
            s3_client.delete_object(Bucket=STATE_BUCKET, Key=_hls_proxy_state_key(token))
        except ClientError:
            pass
        raise HlsProxyError(410, "hls_proxy_session_expired")
    _hls_proxy_kind(session["masterPath"])
    parsed_origin = urlparse(session["origin"])
    if _hls_proxy_origin(parsed_origin) != session["origin"]:
        raise HlsProxyError(502, "hls_proxy_state_invalid")
    query = session["sessionQuery"]
    if (
        len(query) != 1
        or not isinstance(query[0], list)
        or len(query[0]) != 2
        or query[0][0] != "SessionToken"
        or not isinstance(query[0][1], str)
        or len(query[0][1]) < 32
    ):
        raise HlsProxyError(502, "hls_proxy_state_invalid")
    return session

def _new_hls_proxy_session(event, camera_id, playback_mode, hls_url, expires_in):
    parsed = urlparse(hls_url)
    origin = _hls_proxy_origin(parsed)
    if _hls_proxy_kind(parsed.path) != "playlist" or parsed.fragment:
        raise HlsProxyError(502, "hls_upstream_resource_invalid")
    query = [[key, value] for key, value in parse_qsl(parsed.query, keep_blank_values=True)]
    if (
        len(query) != 1
        or query[0][0] != "SessionToken"
        or len(query[0][1]) < 32
    ):
        raise HlsProxyError(502, "hls_upstream_session_invalid")
    token = secrets.token_urlsafe(32)
    if not HLS_PROXY_TOKEN_RE.fullmatch(token):
        raise HlsProxyError(502, "hls_proxy_state_invalid")
    session = {
        "schemaVersion": 1,
        "tokenHash": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "cameraId": camera_id,
        "playbackMode": playback_mode,
        "origin": origin,
        "masterPath": parsed.path,
        "sessionQuery": query,
        "issuedAtEpoch": int(time.time()),
        "expiresAtEpoch": int(time.time()) + int(expires_in),
    }
    _store_hls_proxy_session(token, session)
    return _hls_proxy_url(event, token, "master")

def _encode_hls_resource(session, upstream_url):
    parsed = urlparse(upstream_url)
    if _hls_proxy_origin(parsed) != session["origin"] or parsed.fragment:
        raise HlsProxyError(502, "hls_upstream_resource_invalid")
    kind = _hls_proxy_kind(parsed.path)
    secret_query = [tuple(pair) for pair in session["sessionQuery"]]
    secret_keys = {key for key, _value in secret_query}
    secret_values = {value for _key, value in secret_query}
    public_query = []
    for pair in parse_qsl(parsed.query, keep_blank_values=True):
        if pair in secret_query:
            continue
        if pair[0] in secret_keys or any(secret in pair[1] for secret in secret_values):
            raise HlsProxyError(502, "hls_upstream_resource_invalid")
        public_query.append([pair[0], pair[1]])
    if len(public_query) > 64:
        raise HlsProxyError(502, "hls_upstream_resource_invalid")
    descriptor = json.dumps(
        {"p": parsed.path, "q": public_query, "k": kind},
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(descriptor).decode("ascii").rstrip("=")
    if not 1 <= len(encoded) <= 4096:
        raise HlsProxyError(502, "hls_upstream_resource_invalid")
    signature = hmac.new(
        _hls_proxy_signing_key(session), encoded.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{encoded}.{signature}"

def _decode_hls_resource(session, resource_id):
    if not HLS_PROXY_RESOURCE_RE.fullmatch(resource_id or "") or resource_id == "master":
        raise HlsProxyError(404, "hls_proxy_resource_not_found")
    encoded, signature = resource_id.rsplit(".", 1)
    expected = hmac.new(
        _hls_proxy_signing_key(session), encoded.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HlsProxyError(404, "hls_proxy_resource_not_found")
    try:
        padding = "=" * (-len(encoded) % 4)
        descriptor = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HlsProxyError(404, "hls_proxy_resource_not_found") from exc
    if not isinstance(descriptor, dict) or set(descriptor) != {"p", "q", "k"}:
        raise HlsProxyError(404, "hls_proxy_resource_not_found")
    path = descriptor["p"]
    query = descriptor["q"]
    kind = descriptor["k"]
    if _hls_proxy_kind(path) != kind or not isinstance(query, list) or len(query) > 64:
        raise HlsProxyError(404, "hls_proxy_resource_not_found")
    secret_keys = {pair[0] for pair in session["sessionQuery"]}
    normalized_query = []
    for pair in query:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(value, str) for value in pair)
            or pair[0] in secret_keys
        ):
            raise HlsProxyError(404, "hls_proxy_resource_not_found")
        normalized_query.append((pair[0], pair[1]))
    full_query = [tuple(pair) for pair in session["sessionQuery"]] + normalized_query
    return urlunparse(("https", urlparse(session["origin"]).netloc, path, "", urlencode(full_query), "")), kind

def _fetch_hls_upstream(url, kind):
    max_bytes = HLS_PROXY_PLAYLIST_MAX_BYTES if kind == "playlist" else HLS_PROXY_SEGMENT_MAX_BYTES
    request = Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "v2x-hls-proxy/1"})
    try:
        with HLS_PROXY_OPENER.open(request, timeout=HLS_PROXY_FETCH_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", response.getcode())
            if status != 200:
                raise HlsProxyError(502, "hls_upstream_unavailable")
            encoding = (response.headers.get("content-encoding") or "identity").lower()
            if encoding != "identity":
                raise HlsProxyError(502, "hls_upstream_response_invalid")
            length = response.headers.get("content-length")
            if length:
                try:
                    if int(length) > max_bytes:
                        raise HlsProxyError(502, "hls_upstream_response_too_large")
                except ValueError as exc:
                    raise HlsProxyError(502, "hls_upstream_response_invalid") from exc
            body = response.read(max_bytes + 1)
    except HlsProxyError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise HlsProxyError(502, "hls_upstream_unavailable") from exc
    if len(body) > max_bytes:
        raise HlsProxyError(502, "hls_upstream_response_too_large")
    return body

def _rewrite_hls_playlist(event, token, session, upstream_url, body):
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HlsProxyError(502, "hls_upstream_playlist_invalid") from exc
    if not text.lstrip().startswith("#EXTM3U"):
        raise HlsProxyError(502, "hls_upstream_playlist_invalid")

    def rewrite_uri(value):
        child_url = urljoin(upstream_url, value)
        resource_id = _encode_hls_resource(session, child_url)
        return _hls_proxy_url(event, token, resource_id)

    output = []
    for line in text.splitlines():
        if line.startswith("#"):
            line = HLS_URI_ATTRIBUTE_RE.sub(
                lambda match: f'URI="{rewrite_uri(match.group(1))}"', line
            )
        elif line.strip():
            line = rewrite_uri(line.strip())
        output.append(line)
    rewritten = "\n".join(output) + "\n"
    for _key, secret in session["sessionQuery"]:
        if secret in rewritten:
            raise HlsProxyError(502, "hls_proxy_secret_leak_blocked")
    return rewritten.encode("utf-8")

def _hls_body_response(body, kind):
    if kind == "playlist":
        content_type = "application/vnd.apple.mpegurl; charset=utf-8"
    elif kind == "ts":
        content_type = "video/mp2t"
    else:
        content_type = "video/mp4"
    return {
        "statusCode": 200,
        "headers": {
            "content-type": content_type,
            "content-length": str(len(body)),
            "cache-control": "private, no-store",
            "access-control-allow-origin": "*",
            "x-content-type-options": "nosniff",
        },
        "isBase64Encoded": True,
        "body": base64.b64encode(body).decode("ascii"),
    }

def _get_hls_proxy_resource(event, token, resource_id):
    try:
        session = _load_hls_proxy_session(token)
        if resource_id == "master":
            kind = "playlist"
            upstream_url = urlunparse(
                (
                    "https",
                    urlparse(session["origin"]).netloc,
                    session["masterPath"],
                    "",
                    urlencode([tuple(pair) for pair in session["sessionQuery"]]),
                    "",
                )
            )
        else:
            upstream_url, kind = _decode_hls_resource(session, resource_id)
        body = _fetch_hls_upstream(upstream_url, kind)
        if kind == "playlist":
            body = _rewrite_hls_playlist(event, token, session, upstream_url, body)
        return _hls_body_response(body, kind)
    except HlsProxyError as exc:
        return _hls_proxy_error(exc)

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

def _parse_trusted_ts(value):
    """Parse only explicit timezone-bearing timestamps for trust decisions."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)

def _exact_schema_version(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or not numeric.is_integer():
        return None
    return int(numeric)

def _trusted_media_time(item):
    """Apply the persisted schema-v2 HLS media-time acceptance contract."""
    if item.get("media_time_trusted") is not True:
        return False
    if _exact_schema_version(item.get("timestamp_schema_version")) != 2:
        return False

    timestamp_raw = item.get("timestamp_utc")
    media_timestamp_raw = item.get("media_timestamp_utc")
    if (
        not isinstance(timestamp_raw, str)
        or not timestamp_raw.strip()
        or not isinstance(media_timestamp_raw, str)
        or not media_timestamp_raw.strip()
        or timestamp_raw.strip() != media_timestamp_raw.strip()
    ):
        return False
    media_timestamp = _parse_trusted_ts(media_timestamp_raw)
    if media_timestamp is None:
        return False

    media_clock = item.get("media_clock")
    if not isinstance(media_clock, dict):
        return False
    if media_clock.get("source") != "hls_ext_x_program_date_time":
        return False
    if _exact_schema_version(media_clock.get("schema_version")) != 1:
        return False
    anchor = _parse_trusted_ts(media_clock.get("anchor_program_date_time_utc"))
    position = media_clock.get("position_milliseconds")
    if (
        anchor is None
        or isinstance(position, bool)
        or not isinstance(position, (int, float, Decimal))
    ):
        return False
    try:
        position_ms = float(position)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(position_ms) or position_ms < 0:
        return False
    reconstructed = anchor + timedelta(milliseconds=position_ms)
    return abs((reconstructed - media_timestamp).total_seconds()) * 1000.0 <= 5.0

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

def _get_hls_session(event, camera_id, qs, *, browser_proxy=False):
    if camera_id not in ALLOWED_CAMERA_IDS:
        return _resp(404, {"error": "camera_not_found", "cameraId": camera_id})

    stream_name = _camera_stream_name(camera_id)
    start_dt = _parse_ts(qs.get("start"))
    end_dt = _parse_ts(qs.get("end"))
    on_demand = start_dt is not None or end_dt is not None
    raw_live_fragments = qs.get("max_fragments")
    try:
        live_fragments = 5 if raw_live_fragments is None else int(raw_live_fragments)
    except (TypeError, ValueError):
        return _resp(400, {"error": "invalid_max_fragments", "detail": "must be an integer from 2 through 5"})
    if not 2 <= live_fragments <= 5:
        return _resp(400, {"error": "invalid_max_fragments", "detail": "must be an integer from 2 through 5"})

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
            delivery_url = hls_url
            delivery = "DIRECT_KINESIS"
            if browser_proxy:
                delivery_url = _new_hls_proxy_session(
                    event,
                    camera_id,
                    "ON_DEMAND",
                    hls_url,
                    VIDEO_ONDEMAND_EXPIRES_SECONDS,
                )
                delivery = "SAME_ORIGIN_PROXY"
            return _resp(
                200,
                {
                    "cameraId": camera_id,
                    "streamName": stream_name,
                    "playbackMode": "ON_DEMAND",
                    "hlsUrl": delivery_url,
                    "delivery": delivery,
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
            MaxMediaPlaylistFragmentResults=live_fragments,
        )["HLSStreamingSessionURL"]
        delivery_url = hls_url
        delivery = "DIRECT_KINESIS"
        if browser_proxy:
            delivery_url = _new_hls_proxy_session(
                event,
                camera_id,
                "LIVE",
                hls_url,
                VIDEO_HLS_EXPIRES_SECONDS,
            )
            delivery = "SAME_ORIGIN_PROXY"
        return _resp(
            200,
            {
                "cameraId": camera_id,
                "streamName": stream_name,
                "playbackMode": "LIVE",
                "hlsUrl": delivery_url,
                "delivery": delivery,
                "expiresIn": VIDEO_HLS_EXPIRES_SECONDS,
                "maxMediaPlaylistFragmentResults": live_fragments,
                "region": VIDEO_AWS_REGION,
            },
        )
    except HlsProxyError as exc:
        return _hls_proxy_error(exc)
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

def _get_detections_recent(limit, exclusive_start_key):
    """Return the site's newest detections from the geohash/time index.

    DynamoDB Scan order isn't chronological, and its Limit is applied before
    any client-side sort. Querying the site's shared geohash partition keeps
    pagination stable and guarantees newest-first results without reading old
    table pages first.
    """
    kwargs = {
        "IndexName": GSI_NAME,
        "KeyConditionExpression": Key("geohash").eq(SITE_GEOHASH),
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
        "ProjectionExpression": (
            "event_id, object_id, object_type, timestamp_utc, "
            "media_timestamp_utc, timestamp_schema_version, media_time_trusted, "
            "media_clock, device_id, confidence_score"
        ),
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
            schema_raw = item.get("timestamp_schema_version")
            timestamp_schema_version = _exact_schema_version(schema_raw)
            media_time_trusted = _trusted_media_time(item)
            event_id = str(item.get("event_id") or "")
            media_timestamp = str(item.get("media_timestamp_utc") or "")

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
                    "media_time_trusted": media_time_trusted,
                    "timestamp_schema_version": timestamp_schema_version,
                    "first_event_id": event_id,
                    "last_event_id": event_id,
                    "first_media_timestamp_utc": media_timestamp,
                    "last_media_timestamp_utc": media_timestamp,
                }
            else:
                track["count"] += 1
                track["media_time_trusted"] = (
                    track["media_time_trusted"] and media_time_trusted
                )
                if ts < track["first_seen"]:
                    track["first_seen"] = ts
                    track["first_event_id"] = event_id
                    track["first_media_timestamp_utc"] = media_timestamp
                if ts > track["last_seen"]:
                    track["last_seen"] = ts
                    track["last_event_id"] = event_id
                    track["last_media_timestamp_utc"] = media_timestamp
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
                    "media_time_trusted": t["media_time_trusted"],
                    "timestamp_schema_version": t["timestamp_schema_version"],
                    "first_event_id": t["first_event_id"],
                    "last_event_id": t["last_event_id"],
                    "first_media_timestamp_utc": t["first_media_timestamp_utc"],
                    "last_media_timestamp_utc": t["last_media_timestamp_utc"],
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

    if path.startswith("/video/proxy/"):
        proxy_suffix = path.split("/video/proxy/", 1)[1]
        parts = proxy_suffix.split("/", 1)
        if len(parts) != 2:
            return _resp(404, {"error": "hls_proxy_resource_not_found"})
        token = path_params.get("token") or parts[0]
        resource_id = path_params.get("resource_id") or parts[1]
        return _get_hls_proxy_resource(event, token, resource_id)

    if path.startswith("/video/browser-session/"):
        camera_id = path_params.get("camera_id") or path.split("/video/browser-session/", 1)[1]
        return _get_hls_session(event, camera_id, qs, browser_proxy=True)

    if path.startswith("/video/session/"):
        camera_id = path_params.get("camera_id") or path.split("/video/session/", 1)[1]
        return _get_hls_session(event, camera_id, qs)

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
        return _get_detections_recent(limit, exclusive_start_key)

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
                    "/video/browser-session/{camera_id}",
                    "/video/proxy/{token}/{resource_id}",
                    "/video/coverage/{camera_id}",
                ],
            },
        )

    return _resp(404, {"error": "not_found", "path": path})
