from dataclasses import dataclass
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import math
import os
import re
import tempfile
from urllib.parse import parse_qs, urljoin, urlparse

import boto3
import requests
from dotenv import load_dotenv

from ffmpeg_capture import match_fragment_frame_nvdec

load_dotenv()


def _utc_iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _parse_program_date_time(value):
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("EXT-X-PROGRAM-DATE-TIME must include a timezone")
    return parsed.timestamp()


def _safe_fragment_id(segment_url):
    """Extract the non-credential KVS fragment number from a segment URL."""
    values = parse_qs(urlparse(segment_url).query).get("FragmentNumber") or []
    if not values:
        return None
    value = values[0]
    if not re.fullmatch(r"[A-Za-z0-9._~-]{1,256}", value):
        return None
    return value


@dataclass(frozen=True)
class HlsMediaClock:
    """Map an OpenCV session cursor onto an explicitly matched HLS frame.

    Deliberately stores no playlist or segment URL. Signed Kinesis session
    credentials must remain transient and must never reach detections, logs, or
    object representations.
    """

    anchor_epoch: float
    anchor_program_date_time_utc: str
    anchor_fragment_frame_offset_milliseconds: float
    anchor_capture_position_milliseconds: float
    anchor_fragment_id: str = None
    anchor_media_sequence: int = None
    segment_duration_seconds: float = None

    def metadata_at(self, position_milliseconds):
        position = float(position_milliseconds)
        if not math.isfinite(position) or position < 0:
            return None
        delta = position - self.anchor_capture_position_milliseconds
        if delta < -0.5:
            return None

        metadata = {
            "source": "hls_ext_x_program_date_time",
            "schema_version": 1,
            "anchor_program_date_time_utc": self.anchor_program_date_time_utc,
            "anchor_fragment_frame_offset_milliseconds": round(
                self.anchor_fragment_frame_offset_milliseconds, 3
            ),
            "anchor_capture_position_milliseconds": round(
                self.anchor_capture_position_milliseconds, 3
            ),
            # This is media position from the persisted PDT anchor, not
            # OpenCV's session-relative cursor. The verifier can therefore
            # reconstruct media_timestamp_utc without process-local state.
            "position_milliseconds": round(
                self.anchor_fragment_frame_offset_milliseconds + delta, 3
            ),
            "capture_position_milliseconds": round(position, 3),
        }
        if self.anchor_fragment_id is not None:
            metadata["anchor_fragment_id"] = self.anchor_fragment_id
        if self.anchor_media_sequence is not None:
            metadata["anchor_media_sequence"] = self.anchor_media_sequence
        if self.segment_duration_seconds is not None:
            metadata["segment_duration_seconds"] = self.segment_duration_seconds

        return {
            "media_timestamp_utc": _utc_iso(
                self.anchor_epoch + delta / 1000.0
            ),
            "media_clock": metadata,
        }


def _first_playlist_uri(lines):
    return next(
        (line.strip() for line in lines if line.strip() and not line.startswith("#")),
        None,
    )


@dataclass(repr=False)
class _HlsFragment:
    program_date_time_epoch: float
    program_date_time_utc: str
    duration_seconds: float
    media_sequence: int
    fragment_id: str
    init_url: str
    segment_url: str


def _map_uri(line):
    match = re.search(r'(?:^|,)URI="([^"]+)"', line.split(":", 1)[1])
    if match:
        return match.group(1)
    match = re.search(r"(?:^|,)URI=([^,]+)", line.split(":", 1)[1])
    return None if match is None else match.group(1).strip()


def _fragments_from_media_playlist(playlist_url, text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    media_sequence = None
    init_url = None
    pending_program_date_time = None
    pending_duration = None
    segment_index = 0
    fragments = []

    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except ValueError:
                media_sequence = None
            continue
        if line.startswith("#EXT-X-MAP:"):
            uri = _map_uri(line)
            init_url = None if uri is None else urljoin(playlist_url, uri)
            continue
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_program_date_time = line.split(":", 1)[1]
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending_duration = float(
                    line.split(":", 1)[1].split(",", 1)[0]
                )
            except ValueError:
                pending_duration = None
            continue
        if line.startswith("#"):
            continue

        if pending_program_date_time is not None and init_url is not None:
            anchor_epoch = _parse_program_date_time(pending_program_date_time)
            fragment_url = urljoin(playlist_url, line)
            fragments.append(
                _HlsFragment(
                    program_date_time_epoch=anchor_epoch,
                    program_date_time_utc=_utc_iso(anchor_epoch),
                    duration_seconds=pending_duration,
                    media_sequence=(
                        None
                        if media_sequence is None
                        else media_sequence + segment_index
                    ),
                    fragment_id=_safe_fragment_id(fragment_url),
                    init_url=init_url,
                    segment_url=fragment_url,
                )
            )

        segment_index += 1
        pending_program_date_time = None
        pending_duration = None

    return fragments


def _bounded_content(response, limit=32 * 1024 * 1024):
    content = response.content
    if len(content) > limit:
        raise ValueError("HLS fragment exceeds the safe matching bound")
    return content


def _match_fragment_frame(
    init_bytes,
    segment_bytes,
    target_identity,
    frame_identity,
):
    """Return the exact frame offset within one fMP4 fragment, if present."""
    import cv2

    path = None
    capture = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="v2x-hls-clock-", suffix=".mp4", delete=False
        ) as fragment_file:
            path = fragment_file.name
            fragment_file.write(init_bytes)
            fragment_file.write(segment_bytes)

        capture = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
        if not capture.isOpened():
            return None
        matches = []
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            position = capture.get(cv2.CAP_PROP_POS_MSEC)
            if frame_identity(frame) == target_identity:
                matches.append(float(position))
        if len(matches) == 1:
            return matches[0]
        return None
    finally:
        if capture is not None:
            capture.release()
        if path is not None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def resolve_hls_media_clock(
    hls_url,
    reference_frame,
    capture_position_milliseconds,
    frame_identity,
    timeout=10,
    http_get=requests.get,
    fragment_matcher=_match_fragment_frame,
):
    """Match one decoded frame to its exact HLS PDT/fragment position.

    OpenCV's live ``CAP_PROP_POS_MSEC`` starts at zero even when its first frame
    is late inside an fMP4 fragment, so playlist-start plus that cursor is not a
    valid UTC clock. This function decodes the playlist's bounded fragments and
    matches the actual first frame before establishing an anchor. No match means
    no media timestamp; receipt time is never relabeled as archive time.
    """
    response = http_get(hls_url, timeout=timeout)
    response.raise_for_status()
    lines = [line.strip() for line in response.text.splitlines() if line.strip()]

    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        media_uri = _first_playlist_uri(lines)
        if media_uri is None:
            return None
        playlist_url = urljoin(hls_url, media_uri)
        response = http_get(playlist_url, timeout=timeout)
        response.raise_for_status()
    else:
        playlist_url = hls_url

    fragments = _fragments_from_media_playlist(playlist_url, response.text)
    if not fragments:
        return None

    target_identity = frame_identity(reference_frame)
    init_cache = {}
    for init_url in {fragment.init_url for fragment in fragments}:
        init_response = http_get(init_url, timeout=timeout)
        init_response.raise_for_status()
        init_cache[init_url] = _bounded_content(init_response)

    def download_fragment(fragment):
        segment_response = http_get(fragment.segment_url, timeout=timeout)
        segment_response.raise_for_status()
        return fragment, _bounded_content(segment_response)

    def match_downloaded_fragment(downloaded):
        fragment, segment_bytes = downloaded
        frame_offset = fragment_matcher(
            init_cache[fragment.init_url],
            segment_bytes,
            target_identity,
            frame_identity,
        )
        return None if frame_offset is None else (fragment, frame_offset)

    # Signed KVS fragment requests can each block for several seconds. Fetch
    # the bounded five-fragment LIVE window concurrently, but keep injected
    # unit-test transports deterministic and sequential.
    if (
        len(fragments) > 1
        and http_get is requests.get
        and fragment_matcher is match_fragment_frame_nvdec
    ):
        # A live process already owns one NVDEC session per camera. Fetch the
        # bounded fragment window concurrently, then decode candidates one at
        # a time so four cameras cannot burst into 20 additional GPU decoder
        # sessions during a clock re-anchor.
        with ThreadPoolExecutor(max_workers=min(5, len(fragments))) as executor:
            downloaded = list(executor.map(download_fragment, fragments))
        candidates = [
            match_downloaded_fragment(fragment) for fragment in downloaded
        ]
    elif (
        len(fragments) > 1
        and http_get is requests.get
        and fragment_matcher is _match_fragment_frame
    ):
        with ThreadPoolExecutor(max_workers=min(5, len(fragments))) as executor:
            candidates = list(
                executor.map(
                    lambda fragment: match_downloaded_fragment(
                        download_fragment(fragment)
                    ),
                    fragments,
                )
            )
    else:
        candidates = [
            match_downloaded_fragment(download_fragment(fragment))
            for fragment in fragments
        ]
    matches = [candidate for candidate in candidates if candidate is not None]

    # Static/duplicate imagery can appear at several frame positions or in
    # several fragments. Such an anchor is ambiguous and must fail closed.
    if len(matches) != 1:
        return None
    fragment, frame_offset = matches[0]
    capture_position = float(capture_position_milliseconds)
    if not math.isfinite(capture_position) or capture_position < 0:
        return None
    return HlsMediaClock(
        anchor_epoch=(
            fragment.program_date_time_epoch + frame_offset / 1000.0
        ),
        anchor_program_date_time_utc=fragment.program_date_time_utc,
        anchor_fragment_frame_offset_milliseconds=frame_offset,
        anchor_capture_position_milliseconds=capture_position,
        anchor_fragment_id=fragment.fragment_id,
        anchor_media_sequence=fragment.media_sequence,
        segment_duration_seconds=fragment.duration_seconds,
    )


def resolve_hls_media_clock_nvdec(
    hls_url,
    reference_frame,
    capture_position_milliseconds,
    frame_identity,
    timeout=10,
):
    """Resolve an exact clock using the same pixels as the NVDEC live reader."""
    return resolve_hls_media_clock(
        hls_url,
        reference_frame,
        capture_position_milliseconds,
        frame_identity,
        timeout=timeout,
        fragment_matcher=match_fragment_frame_nvdec,
    )

def _camera_id_from_stream_name(stream_name):
    match = re.search(r"(ch\d+)$", stream_name)
    if match:
        return match.group(1)
    return stream_name

def get_video_session_hls_url(stream_name, max_fragments=4):
    """
    Fetch a live HLS URL through the V2X read API instead of direct Kinesis credentials.
    """
    api_base_url = os.getenv("V2X_VIDEO_SESSION_API_BASE_URL", "").rstrip("/")
    if not api_base_url:
        return None

    camera_id = _camera_id_from_stream_name(stream_name)
    try:
        max_fragments = int(max_fragments)
    except (TypeError, ValueError) as exc:
        raise ValueError("live HLS fragment count must be an integer") from exc
    if not 1 <= max_fragments <= 5:
        raise ValueError("live HLS fragment count must be between 1 and 5")
    response = requests.get(
        f"{api_base_url}/video/session/{camera_id}",
        params={"max_fragments": str(max_fragments)},
        headers={"accept": "application/json"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    return payload["hlsUrl"]

def get_kvs_hls_url(
    stream_name,
    region_name="us-west-2",
    max_fragments=None,
):
    """Return one bounded live session without retaining its signed URL.

    The perception reader may use a one-fragment capture session alongside a
    separate five-fragment exact-clock session.  The wider clock window keeps
    the reference fragment available without forcing the decoder to begin
    several fragments behind the live edge.
    """
    if max_fragments is None:
        max_fragments = os.getenv("V2X_PERCEPTION_LIVE_HLS_FRAGMENTS", "4")
    max_fragments = int(max_fragments)
    if not 1 <= max_fragments <= 5:
        raise ValueError("live HLS fragment count must be between 1 and 5")
    api_hls_url = get_video_session_hls_url(stream_name, max_fragments)
    if api_hls_url:
        return api_hls_url

    aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    
    kvs_client = boto3.client(
        'kinesisvideo',
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        region_name=region_name
    )

    endpoint_response = kvs_client.get_data_endpoint(
        StreamName=stream_name,
        APIName='GET_HLS_STREAMING_SESSION_URL'
    )
    endpoint_url = endpoint_response['DataEndpoint']

    kvs_media_client = boto3.client(
        'kinesis-video-archived-media',
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        region_name=region_name,
        endpoint_url=endpoint_url
    )

    url_response = kvs_media_client.get_hls_streaming_session_url(
        StreamName=stream_name,
        PlaybackMode='LIVE',
        ContainerFormat='FRAGMENTED_MP4',
        DiscontinuityMode='ALWAYS',
        DisplayFragmentTimestamp='ALWAYS',
        MaxMediaPlaylistFragmentResults=max_fragments,
    )

    return url_response['HLSStreamingSessionURL']
