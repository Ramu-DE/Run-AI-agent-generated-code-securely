#!/usr/bin/env bash
# One-command build + deploy for the CI runner orchestrator.
#
# Assumes the code editor's bootstrap has already populated these env vars
# in .bashrc:
#   AWS_REGION, MVM_EXECUTION_ROLE_ARN
#
# And that runner/build-image.sh has written CI_RUNNER_IMAGE_ARN to
# /etc/profile.d/ci-runner-image.sh (sourced automatically on new shells,
# and re-sourced here to be safe).
set -euo pipefail

cd "$(dirname "$0")"

# Pull in CI_RUNNER_IMAGE_ARN if this shell was open before build-image.sh ran.
if [[ -f /etc/profile.d/ci-runner-image.sh ]]; then
  # shellcheck disable=SC1091
  source /etc/profile.d/ci-runner-image.sh
fi

: "${AWS_REGION:?AWS_REGION must be set (should be pre-populated by the workshop bootstrap)}"
: "${MVM_EXECUTION_ROLE_ARN:?MVM_EXECUTION_ROLE_ARN must be set (should be pre-populated by the workshop bootstrap)}"
: "${CI_RUNNER_IMAGE_ARN:?CI_RUNNER_IMAGE_ARN not set. Run runner/build-image.sh first.}"

echo "==> sam build"
sam build

echo "==> sam deploy"
sam deploy \
  --region "${AWS_REGION}" \
  --parameter-overrides \
    "ImageArn=${CI_RUNNER_IMAGE_ARN}" \
    "MvmExecutionRoleArn=${MVM_EXECUTION_ROLE_ARN}"

echo ""
echo "==> outputs"
aws cloudformation describe-stacks \
  --stack-name ci-runner-orchestrator \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" \
  --output table

echo ""
echo "Deploy complete. The 'live' alias now points at the just-published version."
echo "The trigger wiring in trigger/wire-codecommit.sh will bind to this alias."
