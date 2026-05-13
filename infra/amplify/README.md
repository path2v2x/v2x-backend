# Amplify Hosting (`v2x-backend`)

This deploys the SvelteKit digital twin dashboard from `apps/web`.

The dashboard is hosted in Amplify (`us-west-2`) and reads live state assets from the dedicated
`v2x-backend` S3 state bucket in `us-west-1` through the read API.

If that bucket has S3 Public Access Block enabled, a direct S3 `STATE_BASE_URL` will not be browser-readable. In that case, point `STATE_BASE_URL` at a private delivery layer such as CloudFront with OAC or an authenticated API instead of the raw bucket URL.

## Deploy

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
- `CLOUDFLARE_DRIVE_WS_URL` adds the Cloudflare drive WebSocket tunnel to `/drive`
- `TAILSCALE_DRIVE_WS_URL` defaults to `wss://path-b860i-aorus-pro-ice.tail1cad6a.ts.net`

## Destroy

```bash
export AWS_PROFILE="Path-Emerging-Dev-147229569658"
export AWS_REGION="us-west-2"

cd /Users/maikyon/Documents/Programming/v2x-backend/infra/amplify
./destroy.sh
```
