# AWS CLI: `v2x-backend` Provisioning

This folder provisions the `v2x-backend` data plane in **`us-west-1`**:

- MQTT ingest: IoT Core -> `v2x-backend-ingest` -> DynamoDB
- Read API: HTTP API -> `v2x-backend-read`
- Private state bucket for digital twin state + snapshots

## Security note

Do **not** paste AWS access keys into chat or commit them to git.

If you already shared credentials, treat them as compromised:
- If they are temporary STS creds, let them expire and issue new ones.
- If they are long-lived, **deactivate/rotate immediately** in IAM.

## Prereqs

- AWS CLI v2 authenticated (recommended: SSO or a named profile)
- `jq` and `zip` available

## Provision

```bash
cd infra/aws-cli

# Optional: use a specific profile
export AWS_PROFILE="your-profile"

# Region is fixed by default to us-west-1, override if needed:
export AWS_REGION="us-west-1"

./provision.sh
```

Default resource names:

- DynamoDB table: `v2x-backend-detections`
- Ingest Lambda: `v2x-backend-ingest`
- Read Lambda: `v2x-backend-read`
- HTTP API: `v2x-backend-api`
- IoT rule: `v2x_backend_detections_to_ddb`
- IoT policy: `v2x-backend-edge-publish`

### If your role can’t call `iam:*` (common with SSO)

If you see `AccessDenied` for `iam:GetRole` / `iam:CreateRole`, don’t create a new role:

- Create/choose an existing IAM role for Lambda in the AWS console (or via your platform team).
- Ensure it trusts `lambda.amazonaws.com` and has `dynamodb:PutItem` on the table plus basic CloudWatch Logs permissions.
- Run:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 SKIP_IAM=true \
  LAMBDA_ROLE_ARN="arn:aws:iam::<account-id>:role/<existing-role>" \
  ./provision.sh
```

If your principal can’t edit IAM roles but you can `iam:PassRole`, you still need the role to already include DynamoDB permissions.

If you *can* add an inline policy to that existing role (still no new role), you can have the script add `dynamodb:PutItem` for you:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 SKIP_IAM=true \
  LAMBDA_ROLE_ARN="arn:aws:iam::<account-id>:role/<existing-role>" \
  ATTACH_DDB_PUT_POLICY=true \
  ./provision.sh
```

Artifacts:
- Device cert/key written under `./.secrets/iot/<thingName>/` (ignored by git)

## Create another device (Thing + cert)

```bash
cd infra/aws-cli
./create-device.sh edge-device-002
```

Note: `./provision.sh` will not generate new certificates for `THING_NAME` if one is already attached.

## Publish a test event (IAM creds, not device cert)

```bash
cd infra/aws-cli
./publish-test.sh
```

## Read and write API

Provision the write route:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 ./provision-write-api.sh
```

Provision the read routes:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 \
RECONCILE_LAMBDA=true ATTACH_DDB_READ_POLICY=true PLAN_ONLY=true \
  ./provision-read-api.sh
# Review the exact Lambda/integration/route/stage plan and copy its state hash,
# then apply the same inputs explicitly:
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 \
RECONCILE_LAMBDA=true PLAN_ONLY=false \
ATTACH_DDB_READ_POLICY=true \
EXPECTED_CURRENT_STATE_HASH=<reviewed-hash> ./provision-read-api.sh
```

The read provisioner reconciles one managed Lambda proxy integration and each
tracked route in place. It updates an existing route whose target drifted and
does not hide API Gateway failures behind `|| true`. `PLAN_ONLY=true` performs
AWS reads only. Running it repeatedly must report `KEEP`/`REUSE` for converged
routes rather than creating duplicate integrations.

IAM is unchanged by default (`ATTACH_DDB_READ_POLICY=false`). An existing read
Lambda retains its execution role. Creating the read Lambda requires an
explicit, pre-provisioned `READ_LAMBDA_ROLE_ARN`; the script will not infer the
ingest role or create a role. Set `ATTACH_DDB_READ_POLICY=true` only in a
separately approved IAM gate after reviewing the generated least-privilege
inline policy. The same reviewed gate is mandatory when deploying the HLS proxy
Lambda because its opaque session state requires prefix-scoped S3
get/put/delete access. A code apply with `RECONCILE_LAMBDA=true` fails closed
unless that policy reconciliation is explicit. The prior named inline policy
is included in the reviewed state hash and rollback evidence.

For the current recovery, the surgically deployed read Lambda is already the
accepted code/configuration. Reconcile only HTTP API integrations, routes, and
stages:

```bash
AWS_PROFILE=v2x-backend-deploy API_ID=w0j9m7dgpg \
RECONCILE_LAMBDA=false ATTACH_DDB_READ_POLICY=false PLAN_ONLY=true \
  ./provision-read-api.sh
# Copy currentStateHash from the reviewed plan.
AWS_PROFILE=v2x-backend-deploy API_ID=w0j9m7dgpg \
RECONCILE_LAMBDA=false ATTACH_DDB_READ_POLICY=false \
PLAN_ONLY=false EXPECTED_CURRENT_STATE_HASH=<reviewed-hash> ./provision-read-api.sh
```

Every apply captures a mode-0700 rollback directory before its first AWS
mutation. It contains redacted Lambda metadata/configuration/policy, complete
API/integration/route/stage JSON, inputs, and SHA-256 evidence. When
`RECONCILE_LAMBDA=true`, it also immediately downloads the expiring prior
Lambda artifact as `lambda-before.zip`; `Code.Location` is never persisted.
When execution-role policy reconciliation is requested, the prior named inline
policy (or its proven absence) is also retained.
Route-only recovery does not alter Lambda code, configuration, role, or
resource policy.

The dedicated deploy role intentionally has no `apigateway:DELETE`. Existing
route/integration targets can be restored with `PATCH`, while newly created
children remain as additive infrastructure during rollback. Exact deletion
requires a separately reviewed break-glass principal and is not part of this
recovery execution.

Optional env vars for `provision-read-api.sh` include:

- `STATE_BUCKET` (defaults to `v2x-backend-state-<account-id>-us-west-1`)
- `SNAPSHOT_URL_EXPIRES_SECONDS` (defaults to `300`)
- `READ_LAMBDA_ROLE_ARN` (required only when the read Lambda does not exist)
- `ATTACH_DDB_READ_POLICY` (defaults to `false`; explicit IAM mutation)
- `RECONCILE_LAMBDA` (defaults to `false`; set `true` only for a reviewed Lambda deployment)
- `EXPECTED_CURRENT_STATE_HASH` (required for apply; copied from the reviewed plan)
- `PLAN_ONLY` (defaults to `true`; apply requires an explicit `false`)

Tracked read routes include `/drive-config`, `/detections/timeline`,
`/video/coverage/{camera_id}`, and the opaque
`/video/proxy/{token}/{resource_id}` route in addition to the state, snapshot,
detection, demo-video, and HLS-session routes. After applying, capture
route-to-integration parity with:

```bash
api_id="$(aws apigatewayv2 get-apis \
  --query 'Items[?Name==`v2x-backend-api`].ApiId | [0]' --output text)"
aws apigatewayv2 get-routes --api-id "$api_id" \
  --query 'Items[].{route:RouteKey,target:Target}' --output table
aws apigatewayv2 get-integrations --api-id "$api_id" \
  --query 'Items[].{id:IntegrationId,uri:IntegrationUri,description:Description}' --output table
```

### Dedicated API/Lambda deploy role

The recovered IAM user `arn:aws:iam::147229569658:user/rfs-v2x-service`
cannot read API Gateway. Do not add
V2X deployment permissions to `AmplifyServiceRole-OPTIMAT-FRONT-SK`; Amplify's
service role has a separate trust boundary and is not a general deployment
role.

The plan-first bootstrap is deliberately separate from
`provision-read-api.sh`:

```bash
# Local policy rendering/validation only; makes no AWS calls.
ACTION=plan ./bootstrap-v2x-deploy-role.sh

# Read the actual user/role/policy/tag/attachment/profile state without writes.
ACTION=review ./bootstrap-v2x-deploy-role.sh

# Requires the exact state hash from review and a separately authorized IAM
# bootstrap principal.
ACTION=apply EXPECTED_CURRENT_STATE_HASH=<reviewed-hash> \
  ./bootstrap-v2x-deploy-role.sh
```

It creates/reconciles `V2XBackendDeployRole`, whose trust policy names only
that exact IAM user, plus a narrowly scoped inline user policy allowing
`rfs-v2x-service` to assume only that new role. The deploy role can read and
reconcile routes, integrations, and stages under HTTP API `w0j9m7dgpg`, and
update/inspect the existing `v2x-backend-read` Lambda and its API Gateway
invoke permission. It cannot create a Lambda, delete API Gateway resources,
modify IAM, or pass a role. `iam:PassRole` is unnecessary because the existing
read Lambda keeps its current execution role.

The role also has read-only CloudWatch metric access and read-only access to
the exact `/aws/lambda/v2x-backend-read` log group so a release gate can retain
error/throttle and failure-signature evidence. It has no CloudWatch or Logs
write actions and no access to other log groups.

Configure an AWS CLI role profile whose source profile is the existing
`rfs-v2x-service` credential chain, then force the known API ID so the read
provisioner does not need account-wide `GET /apis`:

```ini
[profile v2x-backend-deploy]
role_arn = arn:aws:iam::147229569658:role/V2XBackendDeployRole
source_profile = <rfs-v2x-service-source-profile>
role_session_name = v2x-backend-reconcile
region = us-west-1
```

```bash
AWS_PROFILE=v2x-backend-deploy API_ID=w0j9m7dgpg \
RECONCILE_LAMBDA=false ATTACH_DDB_READ_POLICY=false PLAN_ONLY=true \
  ./provision-read-api.sh
```

`ACTION=review` requires read access to the trusted user, its named inline
policy, the named role and inline policy (when present), role tags, attached and
inline policy inventories, and instance profiles. It prints a canonical
`currentStateHash` but writes nothing persistently. `ACTION=apply` re-reads that
state and refuses to mutate IAM without the matching
`EXPECTED_CURRENT_STATE_HASH`.

`ACTION=apply` writes a mode-0700 rollback directory under
`/home/path/V2XCarla/v2x-backend-backups/iam-bootstrap/` before changing an
existing role or inline policy. To restore an existing role, extract
`.Role.AssumeRolePolicyDocument` from `role.json` and `.PolicyDocument` from
`inline-policy.json`, then use `iam update-assume-role-policy` and
`iam put-role-policy`. Restore or remove the source-user assume policy with
`iam put-user-policy`/`iam delete-user-policy` according
to `source-assume-policy-existed.txt`. If both the deploy role and source-user
assume policy were new, deletion is explicitly gated:

```bash
ACTION=delete CONFIRM_DELETE=V2XBackendDeployRole \
  ./bootstrap-v2x-deploy-role.sh
```

Deletion refuses a deploy role that has an unmanaged inline policy, attached
managed policy, or instance profile. It never deletes the
`rfs-v2x-service` user or touches the Amplify service role; it manages only the
named, single-resource assume policy on that user.

Example write request:

```bash
curl -X POST -H 'content-type: application/json' \
  -d '{"object_id":"traffic_cone_001","timestamp_utc":"2026-02-05T00:00:00Z"}' \
  'https://<api-id>.execute-api.us-west-1.amazonaws.com/detections'
```

Example read request:

```bash
curl 'https://<api-id>.execute-api.us-west-1.amazonaws.com/detections/recent?limit=5'
```

## State bucket

Provision the dedicated private bucket for digital twin state assets:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 ./provision-state-bucket.sh
```

This creates a bucket named `v2x-backend-state-<account-id>-us-west-1` by default and seeds:

- `api/state.json`
- `api/map-data.json`
- S3 Public Access Block enabled for the bucket
- no public bucket policy or public ACLs

The read API serves the browser-facing state assets from this private bucket:

- `GET /state`
- `GET /map-data`
- `GET /snapshots/{object_id}/latest`

Harden an existing state bucket:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 ./harden-state-bucket.sh
```

## Video streams

Provision the Kinesis Video Streams in `us-west-2`:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-2 ./provision-video-streams.sh
```

Defaults:

- Stream prefix: `v2x-backend-cam-`
- Camera IDs: `ch1 ch2 ch3 ch4`
- Retention: `24` hours requested; existing streams are left at their current retention if already higher

The read API also exposes:

- `GET /video/session/{camera_id}`
- `GET /video/proxy/{token}/{resource_id}` (opaque URLs returned by the session endpoint)
- `GET /video/coverage/{camera_id}?start=<ISO-8601>&end=<ISO-8601>`

The session endpoint returns a short-lived same-origin proxy URL for `ch1`
through `ch4`; it never returns the signed Kinesis URL. The Lambda stores the
Kinesis session token in a private, encrypted `hls-proxy/v1/` state object,
rewrites master and media playlists recursively, and authenticates each opaque
child descriptor with a key derived from that unexposed session token. Proxy
requests accept only the exact Kinesis Video origin and HLS resource allowlist,
reject redirects, and cap playlists at 1 MiB and binary fragments at 4 MiB.
Live sessions default to five minutes; archived sessions require both `start`
and `end` and are limited to a 24-hour window. Expired proxy state is rejected
and deleted when accessed. A bucket lifecycle rule for the dedicated
`hls-proxy/v1/` prefix remains recommended as defense-in-depth cleanup for
expired sessions that are never requested again.

The 4 MiB raw-fragment limit is below Lambda's synchronous base64 response
ceiling. Treat an observed larger fragment as a failed release gate requiring
lower producer fragment size/bitrate or a streaming proxy service; do not raise
the limit past the Lambda/API Gateway transport bound.

An HTTP 200 or advancing MJPEG byte count does not prove a live source. HLS
acceptance requires all four camera producer timestamps to remain recent and
advance across two samples, real frame content to change, and the perception
service to recover after a forced session expiry without accumulating
`CLOSE_WAIT` sockets. The returned `hlsUrl` is safe to identify as a proxy URL,
but do not retain the opaque token path in durable evidence:

```bash
for camera in ch1 ch2 ch3 ch4; do
  curl -fsS "https://<api-id>.execute-api.us-west-1.amazonaws.com/video/session/${camera}" \
    | jq '{cameraId,playbackMode,delivery,expiresIn,hlsUrlPresent:(.hlsUrl|type == "string" and length > 0)}'
done
```

## Cleanup

```bash
cd infra/aws-cli
./cleanup.sh
```

## Legacy decommission

After the new stack is verified, remove the old `v2x-detections-*` and `v2x-viewer` resources:

```bash
cd infra/aws-cli
./decommission-legacy-v2x.sh
```
