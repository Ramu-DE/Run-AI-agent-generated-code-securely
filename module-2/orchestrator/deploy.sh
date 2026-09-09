#!/usr/bin/env bash
# One-command build + deploy for the durable orchestrator.
# Replaces Steps 4-6 of Module 2's durable-orchestration lab.
#
# Assumes the code editor's bootstrap has already populated these env vars
# in .bashrc:
#   AWS_REGION, AWS_ACCOUNTID, FUNCTION_NAME, MVM_EXECUTION_ROLE_ARN
#
# And that reviewer-async/build-image.sh has written IMAGE_ARN to
# /etc/profile.d/mvm-image-arn.sh (sourced automatically on new shells,
# and re-sourced here to be safe).
set -euo pipefail

cd "$(dirname "$0")"

# Pull in IMAGE_ARN if this shell was open before build-image.sh ran.
if [[ -f /etc/profile.d/mvm-image-arn.sh ]]; then
  # shellcheck disable=SC1091
  source /etc/profile.d/mvm-image-arn.sh
fi

: "${AWS_REGION:?AWS_REGION must be set (should be pre-populated by the workshop bootstrap)}"
: "${MVM_EXECUTION_ROLE_ARN:?MVM_EXECUTION_ROLE_ARN must be set (should be pre-populated by the workshop bootstrap)}"
: "${IMAGE_ARN:?IMAGE_ARN not set. Run reviewer-async/build-image.sh first.}"

echo "==> sam build"
sam build

echo "==> sam deploy"
sam deploy \
  --region "${AWS_REGION}" \
  --parameter-overrides \
    "ImageArn=${IMAGE_ARN}" \
    "MvmExecutionRoleArn=${MVM_EXECUTION_ROLE_ARN}"

echo ""
echo "==> outputs"
aws cloudformation describe-stacks \
  --stack-name durable-orchestrator \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" \
  --output table

echo ""
echo "Deploy complete. The 'live' alias now points at the just-published version."
echo "The trigger wiring in trigger/wire-codecommit.sh will bind to this alias."
