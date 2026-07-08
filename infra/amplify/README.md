# Amplify Hosting (`v2x-backend`)

This deploys the SvelteKit digital twin dashboard from `apps/web`.

The dashboard is hosted in Amplify (`us-west-2`) and reads live state assets from the dedicated
`v2x-backend` S3 state bucket in `us-west-1` through the read API.

If that bucket has S3 Public Access Block enabled, a direct S3 `STATE_BASE_URL` will not be browser-readable. In that case, point `STATE_BASE_URL` at a private delivery layer such as CloudFront with OAC or an authenticated API instead of the raw bucket URL.

## Deploy

Connected-repository Amplify deploys use `infra/amplify/buildspec.yml`. The build spec writes
`build/config.json` from Amplify branch environment variables after the SvelteKit build.

```bash
export AWS_PROFILE="Path-Emerging-Dev-147229569658"
export AWS_REGION="us-west-2"
export API_BASE_URL="https://<api-id>.execute-api.us-west-1.amazonaws.com"
cd /Users/maikyon/Documents/Programming/v2x-backend/infra/amplify
./deploy.sh
```

Optional:

- `APP_NAME` defaults to `v2x-backend`
- `BRANCH_NAME` defaults to `main`
- `STATE_BASE_URL` defaults to `API_BASE_URL`
- `STATE_BUCKET` is only needed if you explicitly want to point the dashboard at raw bucket URLs
- `STATE_PATH` defaults to `/state`
- `MAP_DATA_PATH` defaults to `/map-data`
- `PERCEPTION_STREAM_URLS` is optional JSON keyed by camera ID, for example
  `{"ch1":"https://path-pc.example/ch1/index.m3u8"}`
- `PERCEPTION_STREAM_BASE_URL` is optional and builds camera URLs from
  `PERCEPTION_STREAM_PATH_TEMPLATE`, for example `https://perception.path2v2x.net`
- `PERCEPTION_STREAM_PATH_TEMPLATE` defaults to `/streams/{camera_id}.mjpg`
- `CLOUDFLARE_DRIVE_WS_URL` adds a static Cloudflare drive WebSocket tunnel to `/drive`; leave it empty when using `/drive-config`
- `TAILSCALE_DRIVE_WS_URL` defaults to `wss://path-b860i-aorus-pro-ice.tail1cad6a.ts.net`

## Drive WebSocket endpoint

Preferred long term, the deployed dashboard can use a named Cloudflare Tunnel hostname for the public Drive WebSocket:

```text
wss://drive.path2v2x.net
```

For the current no-DNS-change deployment, the Path PC publishes the active Quick Tunnel URL by
updating Amplify branch environment variables and triggering a connected-repo release. This is slower
than a named tunnel or a pure runtime-config object, but it works with the current IAM permissions and
does not require moving `path2v2x.net` DNS.

Publish the current tunnel URL from the Path PC:

```bash
cd /home/path/V2XCarla/v2x-backend
scripts/publish-drive-amplify-config.sh
```

Verify:

```bash
curl https://path2v2x.net/config.json
```

## Destroy

```bash
export AWS_PROFILE="Path-Emerging-Dev-147229569658"
export AWS_REGION="us-west-2"

cd /Users/maikyon/Documents/Programming/v2x-backend/infra/amplify
./destroy.sh
```
