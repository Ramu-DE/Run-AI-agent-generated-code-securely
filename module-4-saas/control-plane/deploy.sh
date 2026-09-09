#!/usr/bin/env bash
# One-command build + deploy for the SaaS control plane.
#
# Assumes the code editor bootstrap populated AWS_REGION and
# MVM_EXECUTION_ROLE_ARN, and that tenant-app/build-image.sh wrote
# SAAS_IMAGE_ARN to /etc/profile.d/saas-image.sh.
set -euo pipefail

cd "$(dirname "$0")"

# Pull in SAAS_IMAGE_ARN if this shell was open before build-image.sh ran.
if [[ -f /etc/profile.d/saas-image.sh ]]; then
  # shellcheck disable=SC1091
  source /etc/profile.d/saas-image.sh
fi

: "${AWS_REGION:?AWS_REGION must be set (should be pre-populated by the workshop bootstrap)}"
: "${MVM_EXECUTION_ROLE_ARN:?MVM_EXECUTION_ROLE_ARN must be set (should be pre-populated by the workshop bootstrap)}"
: "${SAAS_IMAGE_ARN:?SAAS_IMAGE_ARN not set. Run tenant-app/build-image.sh first.}"

echo "==> sam build"
sam build

echo "==> sam deploy"
sam deploy \
  --region "${AWS_REGION}" \
  --parameter-overrides \
    "ImageArn=${SAAS_IMAGE_ARN}" \
    "MvmExecutionRoleArn=${MVM_EXECUTION_ROLE_ARN}"

echo ""
echo "==> control plane URL"
aws cloudformation describe-stacks \
  --stack-name saas-control-plane \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='ControlPlaneUrl'].OutputValue" \
  --output text
