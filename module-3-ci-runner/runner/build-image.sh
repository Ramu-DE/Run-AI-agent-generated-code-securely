#!/usr/bin/env bash
# One-command image build for the Module 3 ephemeral CI runner.
#
# Prereqs (all pre-populated by the workshop's code editor bootstrap):
#   AWS_REGION, AWS_ACCOUNTID, ARTIFACTS_BUCKET, MODULE2_REVIEWER_BUILD_ROLE_ARN
#
# On success, persists CI_RUNNER_IMAGE_ARN to /etc/profile.d/ci-runner-image.sh
# so every subsequent shell (and orchestrator/deploy.sh) picks it up.
set -euo pipefail

cd "$(dirname "$0")"

: "${AWS_REGION:?AWS_REGION must be set (should be pre-populated by the workshop bootstrap)}"
: "${AWS_ACCOUNTID:?AWS_ACCOUNTID must be set (should be pre-populated by the workshop bootstrap)}"
: "${MODULE2_REVIEWER_BUILD_ROLE_ARN:?MODULE2_REVIEWER_BUILD_ROLE_ARN must be set (should be pre-populated by the workshop bootstrap)}"

ARTIFACTS_BUCKET="${ARTIFACTS_BUCKET:-lambda-mvm-workshop-artifacts-${AWS_ACCOUNTID}}"
IMAGE_NAME="${IMAGE_NAME:-mvm-ci-runner}"
IMAGE_VERSION="${IMAGE_VERSION:-1.0}"

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
S3_KEY="deployments/${IMAGE_NAME}-${TIMESTAMP}.zip"

echo "==> zipping source"
rm -f runner.zip
zip -qr runner.zip app.py Dockerfile

echo "==> uploading to s3://${ARTIFACTS_BUCKET}/${S3_KEY}"
aws s3 cp runner.zip "s3://${ARTIFACTS_BUCKET}/${S3_KEY}" \
  --region "${AWS_REGION}"

echo "==> creating microvm image"
RESP=$(aws lambda-microvms create-microvm-image \
  --name "${IMAGE_NAME}" \
  --code-artifact "uri=s3://${ARTIFACTS_BUCKET}/${S3_KEY}" \
  --base-image-arn "arn:aws:lambda:${AWS_REGION}:aws:microvm-image:al2023-1" \
  --build-role-arn "${MODULE2_REVIEWER_BUILD_ROLE_ARN}" \
  --region "${AWS_REGION}" \
  --hooks '{"port":9000,"microvmHooks":{"run":"ENABLED","runTimeoutInSeconds":5,"terminate":"ENABLED","terminateTimeoutInSeconds":5},"microvmImageHooks":{"ready":"ENABLED","readyTimeoutInSeconds":60}}' \
  --egress-network-connectors "arn:aws:lambda:${AWS_REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS" \
  --resources '[{"minimumMemoryInMiB":2048}]' \
  --logging "{\"cloudWatch\":{\"logGroup\":\"/aws/lambda-microvms/${IMAGE_NAME}\"}}")

IMAGE_ARN=$(echo "$RESP" | jq -r '.imageArn')
if [[ -z "${IMAGE_ARN}" || "${IMAGE_ARN}" == "null" ]]; then
  echo "ERROR: create-microvm-image returned no imageArn"
  echo "Response: $RESP"
  exit 1
fi
echo "    imageArn=${IMAGE_ARN}"

echo "==> waiting for build to complete (typically 90-120s)"
while true; do
  STATE=$(aws lambda-microvms get-microvm-image-version \
    --image-identifier "${IMAGE_ARN}" \
    --image-version "${IMAGE_VERSION}" \
    --region "${AWS_REGION}" \
    --query 'state' --output text 2>/dev/null || echo "PENDING")
  echo "    state=${STATE}"
  case "${STATE}" in
    SUCCESSFUL) break ;;
    FAILED)     echo "Image build FAILED"; exit 1 ;;
    *)          sleep 10 ;;
  esac
done

# Persist for downstream scripts and future shells.
sudo tee /etc/profile.d/ci-runner-image.sh >/dev/null <<EOF
export CI_RUNNER_IMAGE_ARN="${IMAGE_ARN}"
EOF
sudo chmod 644 /etc/profile.d/ci-runner-image.sh
export CI_RUNNER_IMAGE_ARN="${IMAGE_ARN}"

echo ""
echo "Runner image built: ${IMAGE_ARN}"
echo "Persisted to /etc/profile.d/ci-runner-image.sh for future shells."
