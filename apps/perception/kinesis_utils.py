import os
import re
import boto3
import requests
from dotenv import load_dotenv

load_dotenv()

def _camera_id_from_stream_name(stream_name):
    match = re.search(r"(ch\d+)$", stream_name)
    if match:
        return match.group(1)
    return stream_name

def get_video_session_hls_url(stream_name):
    """
    Fetch a live HLS URL through the V2X read API instead of direct Kinesis credentials.
    """
    api_base_url = os.getenv("V2X_VIDEO_SESSION_API_BASE_URL", "").rstrip("/")
    if not api_base_url:
        return None

    camera_id = _camera_id_from_stream_name(stream_name)
    response = requests.get(
        f"{api_base_url}/video/session/{camera_id}",
        headers={"accept": "application/json"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    return payload["hlsUrl"]

def get_kvs_hls_url(stream_name, region_name="us-west-2"):
    """
    Fetches a live HLS streaming session URL for a given Kinesis Video Stream.
    """
    api_hls_url = get_video_session_hls_url(stream_name)
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
        PlaybackMode='LIVE'
    )

    return url_response['HLSStreamingSessionURL']
