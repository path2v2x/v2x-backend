# Amplify Hosting (`v2x-backend`)

This deploys the SvelteKit digital twin dashboard from `apps/web`.

The dashboard is hosted in Amplify (`us-west-2`) and reads live state assets from the dedicated
`v2x-backend` S3 state bucket in `us-west-1` through the read API.

If that bucket has S3 Public Access Block enabled, a direct S3 `STATE_BASE_URL` will not be browser-readable. In that case, point `STATE_BASE_URL` at a private delivery layer such as CloudFront with OAC or an authenticated API instead of the raw bucket URL.

## Deployment paths

Connected-repository Amplify deploys use `infra/amplify/buildspec.yml`. The build spec writes
`build/config.json` from Amplify branch environment variables after the SvelteKit build.
For this recovery, do not use `deploy.sh`: use only
`reconcile-repository.sh` followed by `publish-amplify-runtime-config.sh` so
repository authorization and endpoint releases remain separate, hash-gated
acceptance steps.

`deploy.sh` remains a general/manual deployment tool. Its connected-repository
path is recovery-locked: it refuses to run unless the repository is canonical,
both the app metadata and branch-environment hashes match reviewed values, and
`RECOVERY_CONNECTED_DEPLOY_GATE=canonical-reviewed-release` is explicit. It
captures mode-0600 rollback metadata first, preserves variables it does not
manage, fails on timeout, and never implicitly cancels an in-progress job.

```bash
export AWS_PROFILE="Path-Emerging-Dev-147229569658"
export AWS_REGION="us-west-2"
export API_BASE_URL="https://<api-id>.execute-api.us-west-1.amazonaws.com"
cd /Users/maikyon/Documents/Programming/v2x-backend/infra/amplify
./deploy.sh
```

The example above is not the V2X recovery release command. A reviewed exception
for a connected app must additionally set `EXPECTED_APP_METADATA_HASH` and
`EXPECTED_BRANCH_ENV_HASH`; the tool prints observed hashes when the gate is
missing.

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

## Canonical repository reconciliation

The app currently records the pre-transfer repository
`https://github.com/michaelvu1207/v2x-backend`; the canonical target is
`https://github.com/path2v2x/v2x-backend`. Reconcile repository authorization
before changing any Amplify IAM service role:

```bash
# AWS reads only; prints the current metadata hash and exact transition.
ACTION=plan ./reconcile-repository.sh

# Token comes from the environment but is unset before AWS is invoked. The AWS
# CLI reads it from a mode-0600 temporary JSON file, never a process argument.
AMPLIFY_GITHUB_ACCESS_TOKEN='<short-lived-token>' \
ACTION=apply EXPECTED_CURRENT_HASH=<plan-hash> START_RELEASE=false \
  ./reconcile-repository.sh

# Alternatively pipe the token without echoing it.
printf '%s\n' "$TOKEN" | TOKEN_FROM_STDIN=true \
ACTION=apply EXPECTED_CURRENT_HASH=<plan-hash> START_RELEASE=false \
  ./reconcile-repository.sh
```

The script backs up app and branch metadata before update and verifies the
repository returned by `get-app`. It never includes `IamServiceRoleArn` in the
update request. Metadata parity alone does not prove clone access; after review,
opt into one release and require `SUCCEED`:

```bash
ACTION=apply START_RELEASE=true WAIT_FOR_RELEASE=true \
  ./reconcile-repository.sh
```

Rollback requires the reviewed old-repository metadata backup and a token that
can authorize that repository:

```bash
AMPLIFY_GITHUB_ACCESS_TOKEN='<rollback-token>' \
ACTION=rollback ROLLBACK_METADATA_FILE=<backup.json> START_RELEASE=false \
  ./reconcile-repository.sh
```

Only after the canonical clone/build test succeeds should a separate IAM audit
decide whether Amplify needs a service role. Never reuse or contaminate the
dedicated V2X API/Lambda deploy role for Amplify.

If organization-admin access is temporarily unavailable, Amplify may instead
use the owner-controlled `michaelvu1207/v2x-backend-amplify` production mirror.
The mirror is acceptable only when its `main` commit exactly matches
`path2v2x/v2x-backend` and its active push webhook is proven before release.
Pass that repository explicitly rather than changing the reconciliation
script's canonical default:

```bash
CANONICAL_REPOSITORY=https://github.com/michaelvu1207/v2x-backend-amplify \
  ACTION=plan ./reconcile-repository.sh
```

`.github/workflows/sync-amplify-mirror.yml` runs only in the mirror repository.
It uses the repository-scoped `GITHUB_TOKEN`, fetches public canonical `main`,
and permits only a fast-forward of mirror `main`; divergence fails closed and
never rewrites history. The workflow is tracked in canonical source and must be
present at the same commit in the mirror, so production does not depend on
mirror-only source or a separately stored GitHub credential. Direct Amplify
authorization for the organization repository remains the preferred endpoint
once an organization owner can grant webhook administration.

## Runtime endpoints and release gate

Preferred long term, the deployed dashboard can use a named Cloudflare Tunnel hostname for the public Drive WebSocket:

```text
wss://drive.path2v2x.net
```

Quick Tunnel endpoints for both Drive and perception are process-scoped. The
tracked publisher preserves the complete Amplify branch environment, validates
the candidate Drive WebSocket and all four perception streams, writes a
mode-0600 rollback snapshot, and uses a current-environment hash as an
optimistic concurrency gate.

Its default is a read-only plan. Updating branch variables is intentionally
separate from starting a connected-repository release: the current connected
repository/IAM path must be repaired and proven before a release can be
authorized.

```bash
cd /home/path/V2XCarla/v2x-backend

# No Amplify writes.
ACTION=plan scripts/publish-amplify-runtime-config.sh

# Stage endpoint variables while the connected repo is unhealthy; no release.
ACTION=publish START_RELEASE=false EXPECTED_CURRENT_HASH=<hash-from-plan> \
  scripts/publish-amplify-runtime-config.sh

# Production gate only after canonical repository and IAM role-assumption proof.
ACTION=publish START_RELEASE=true EXPECTED_CURRENT_HASH=<hash-from-plan> \
  scripts/publish-amplify-runtime-config.sh
```

`scripts/publish-drive-amplify-config.sh` remains a backward-compatible
Drive-only wrapper and inherits the safe `ACTION=plan` and
`START_RELEASE=false` defaults. A branch-variable update without a successful
release does not alter public `config.json`.

The independent `/drive-config` S3 publisher records prior absence before its
first write. A version-gated rollback to that marker publishes a higher-version
expired tombstone rather than deleting audit history; the web client rejects it
and falls back to static configuration.

Environment rollback defaults to `ROLLBACK_ENDPOINT_MODE=preserve-current`.
This restores non-endpoint variables without resurrecting a dead
process-scoped Quick Tunnel URL. Publish the currently supervised healthy
endpoint afterward. `exact-named` rollback is only for stable named hostnames;
the publisher rejects `*.trycloudflare.com` values in that mode.

Before the production release, record and verify:

- the Amplify app repository is the canonical transferred repository;
- the service role exists and Amplify can assume it;
- branch environment and buildspec are the reviewed versions;
- the intended commit is the release source;
- a rollback environment snapshot and the last known-good deployed job/commit
  are recorded.

Verify:

```bash
curl https://path2v2x.net/config.json
```

Require the release job to reach `SUCCEED`; timeout, `FAILED`, or `CANCELLED`
is a failed deployment. Refresh `/drive`, `/live`, and `/timeline` after release
and capture console/network evidence rather than accepting the job status alone.

## Destroy

```bash
export AWS_PROFILE="Path-Emerging-Dev-147229569658"
export AWS_REGION="us-west-2"

cd /Users/maikyon/Documents/Programming/v2x-backend/infra/amplify
./destroy.sh
```
