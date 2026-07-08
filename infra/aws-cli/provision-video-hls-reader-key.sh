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

AWS_REGION="${AWS_REGION:-us-west-2}"
STREAM_PREFIX="${STREAM_PREFIX:-v2x-backend-cam-}"
CAMERAS="${CAMERAS:-ch1 ch2 ch3 ch4}"
IAM_USER_NAME="${IAM_USER_NAME:-v2x-backend-hls-reader}"
POLICY_NAME="${POLICY_NAME:-v2x-backend-hls-reader}"
CREATE_ACCESS_KEY="${CREATE_ACCESS_KEY:-true}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${OUTPUT_DIR:-${HERE}/.secrets/hls-reader}"

mkdir -p "${OUTPUT_DIR}"
chmod 700 "${OUTPUT_DIR}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

echo "Region: ${AWS_REGION}"
echo "Account: ${ACCOUNT_ID}"
echo "IAM user: ${IAM_USER_NAME}"

STREAMS_JSON="$(mktemp)"
trap 'rm -f "${STREAMS_JSON}" "${STREAMS_JSON}.next"' EXIT
printf '[]' > "${STREAMS_JSON}"

for camera_id in ${CAMERAS}; do
  stream_name="${STREAM_PREFIX}${camera_id}"
  echo "Verifying stream: ${stream_name}"
  stream_info="$(aws kinesisvideo describe-stream \
    --region "${AWS_REGION}" \
    --stream-name "${stream_name}" \
    --output json)"

  item="$(jq -c --arg camera_id "${camera_id}" '
    .StreamInfo
    | {
        CameraId: $camera_id,
        Name: .StreamName,
        ARN: .StreamARN,
        Status: .Status,
        Retention: .DataRetentionInHours
      }
  ' <<<"${stream_info}")"

  jq --argjson item "${item}" '. + [$item]' "${STREAMS_JSON}" > "${STREAMS_JSON}.next"
  mv "${STREAMS_JSON}.next" "${STREAMS_JSON}"
done

cp "${STREAMS_JSON}" "${OUTPUT_DIR}/streams.json"
chmod 600 "${OUTPUT_DIR}/streams.json"

stream_arns="$(jq '[.[].ARN]' "${STREAMS_JSON}")"

jq -n --argjson stream_arns "${stream_arns}" '{
  Version: "2012-10-17",
  Statement: [
    {
      Effect: "Allow",
      Action: [
        "kinesisvideo:DescribeStream",
        "kinesisvideo:GetDataEndpoint"
      ],
      Resource: $stream_arns
    },
    {
      Effect: "Allow",
      Action: [
        "kinesisvideo:GetHLSStreamingSessionURL"
      ],
      Resource: "*"
    },
    {
      Effect: "Allow",
      Action: [
        "kinesisvideo:ListStreams"
      ],
      Resource: "*"
    }
  ]
}' > "${OUTPUT_DIR}/policy.json"
chmod 600 "${OUTPUT_DIR}/policy.json"

if aws iam get-user --user-name "${IAM_USER_NAME}" >/dev/null 2>&1; then
  echo "IAM user already exists"
else
  aws iam create-user --user-name "${IAM_USER_NAME}" >/dev/null
  echo "Created IAM user"
fi

aws iam put-user-policy \
  --user-name "${IAM_USER_NAME}" \
  --policy-name "${POLICY_NAME}" \
  --policy-document "file://${OUTPUT_DIR}/policy.json" >/dev/null

echo "Attached inline policy: ${POLICY_NAME}"

if [[ "${CREATE_ACCESS_KEY}" == "true" ]]; then
  key_json="$(aws iam create-access-key --user-name "${IAM_USER_NAME}" --output json)"
  printf '%s\n' "${key_json}" > "${OUTPUT_DIR}/access-key.json"
  chmod 600 "${OUTPUT_DIR}/access-key.json"

  access_key_id="$(jq -r '.AccessKey.AccessKeyId' <<<"${key_json}")"
  secret_access_key="$(jq -r '.AccessKey.SecretAccessKey' <<<"${key_json}")"

  cat > "${OUTPUT_DIR}/credentials" <<CREDENTIALS
[${IAM_USER_NAME}]
aws_access_key_id = ${access_key_id}
aws_secret_access_key = ${secret_access_key}
CREDENTIALS
  chmod 600 "${OUTPUT_DIR}/credentials"

  cat > "${OUTPUT_DIR}/env" <<ENV
AWS_ACCESS_KEY_ID=${access_key_id}
AWS_SECRET_ACCESS_KEY=${secret_access_key}
AWS_REGION=${AWS_REGION}
AWS_DEFAULT_REGION=${AWS_REGION}
ENV
  chmod 600 "${OUTPUT_DIR}/env"

  echo "Created access key and wrote credentials to: ${OUTPUT_DIR}"
else
  echo "CREATE_ACCESS_KEY=false; skipped access-key creation"
fi

echo
echo "Stream names and ARNs:"
jq -r '.[] | "- \(.CameraId): \(.Name)  \(.ARN)"' "${STREAMS_JSON}"
echo
echo "Secret material was written under ${OUTPUT_DIR}; do not paste it into chat or commit it."
