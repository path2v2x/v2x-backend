# v2x-backend

Canonical repo for the V2X ingest API, CARLA bridge, and digital twin dashboard.

## Repo Structure

```
apps/
  bridge/    Python bridge — connects CARLA to the V2X platform
  web/       SvelteKit dashboard deployed to Amplify
scripts/
  launch-drive.sh   Start the drive server on the GPU server
infra/
  aws-cli/   Provision DynamoDB, Lambda, IoT Core, API Gateway, S3, KVS
  amplify/   Deploy the web dashboard to AWS Amplify
```

## Quick Start

```bash
# Install
make web-install
make bridge-install

# Develop
make web-dev          # SvelteKit dev server
make bridge-dry-run   # Bridge dry-run (no CARLA needed)

# Deploy
make deploy-web       # Deploy dashboard to Amplify
```

## Canonical Workflow

1. Provision the backend data plane and API:

```bash
cd infra/aws-cli
./provision.sh
./provision-read-api.sh
./provision-write-api.sh
./provision-state-bucket.sh
AWS_REGION=us-west-2 ./provision-video-streams.sh
```

2. Deploy the dashboard:

```bash
cd infra/amplify
API_BASE_URL="https://<api-id>.execute-api.us-west-1.amazonaws.com" \
./deploy.sh
```

3. Run the bridge (drive mode):

```bash
./scripts/launch-drive.sh
```

Or manually:

```bash
cd apps/bridge
source /path/to/carla-venv/bin/activate
DTB_V2X_API_URL="https://<api-id>.execute-api.us-west-1.amazonaws.com/detections/recent" \
python -m digital_twin_bridge.drive_main
```

## Runtime Config

`apps/web/static/config.json` and the Amplify deployment expect:

```json
{
  "apiBaseUrl": "https://<api-id>.execute-api.us-west-1.amazonaws.com",
  "stateBaseUrl": "https://<api-id>.execute-api.us-west-1.amazonaws.com",
  "statePath": "/state",
  "mapDataPath": "/map-data",
  "videoCameraIds": ["ch1", "ch2", "ch3", "ch4"],
  "perceptionStreamUrls": {},
  "perceptionStreamBaseUrl": "",
  "perceptionStreamPathTemplate": "/streams/{camera_id}.mjpg"
}
```

The dashboard reads digital twin state and snapshot assets through the read API. The state bucket can remain private because the browser no longer needs direct S3 access.

## Live Video

- Kinesis Video Streams are provisioned in `us-west-2`
- Camera stream names default to: `v2x-backend-cam-ch1` through `v2x-backend-cam-ch4`
- The API exposes `GET /video/session/{camera_id}` and returns a short-lived HLS URL
- The dashboard requests HLS sessions through the API; browser clients do not use AWS credentials directly
- `/live` prefers `perceptionStreamUrls[cameraId]` when configured, then falls back to the raw
  Kinesis HLS session. It can also build URLs from `perceptionStreamBaseUrl`, using
  `/streams/{camera_id}.mjpg` by default. Use this for Path PC object-detection output with bounding boxes:

```json
{
  "perceptionStreamBaseUrl": "https://perception.path2v2x.net"
}
```

The perception service should upload detection records to `POST /detections` using the same schema shown
on the Objects DB documentation tab. The `/live` page shows the recent Objects DB table below the camera
grid, so detections posted by the Path PC will appear there without leaving Street View.

## Perception App

The object detection/localization pipeline from `path2v2x/co-perception` now lives in
`apps/perception`. Run it from this repo on the Path PC with:

```bash
python3.10 -m venv /home/path/V2XCarla/perception-venv
/home/path/V2XCarla/perception-venv/bin/pip install -r apps/perception/requirements.txt
sudo install -m 0755 scripts/launch-perception.sh /home/path/V2XCarla/v2x-backend/scripts/launch-perception.sh
sudo install -m 0644 scripts/systemd/v2x-perception.service /etc/systemd/system/v2x-perception.service
sudo systemctl daemon-reload
sudo systemctl enable --now v2x-perception.service
```

The service starts with `V2X_PERCEPTION_UPLOAD=false`; enable uploads after validating calibration and
model output. With `V2X_PERCEPTION_STREAM_PORT=8090`, the service publishes:

```text
http://<path-pc-host>:8090/streams/ch1.mjpg
http://<path-pc-host>:8090/streams/ch2.mjpg
http://<path-pc-host>:8090/streams/ch3.mjpg
http://<path-pc-host>:8090/streams/ch4.mjpg
```

## GPU Server

The drive server runs on the GPU server (`100.72.252.40` via Tailscale). After pulling changes:

```bash
cd /home/path/V2XCarla/v2x-backend
git pull
./scripts/launch-drive.sh
```

## Notes

- The MQTT topic pattern remains `v2x/v1/detections/+/+`.
- `infra/aws-cli/decommission-legacy-v2x.sh` is the post-cutover cleanup entrypoint for the old `v2x-detections-*` stack.
