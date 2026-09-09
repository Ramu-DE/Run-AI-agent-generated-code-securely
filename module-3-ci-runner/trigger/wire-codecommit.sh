#!/usr/bin/env bash
# One-command CodeCommit trigger wire-up for the Module 3 CI runner lab.
#
# Assumes:
#   - The orchestrator has been deployed via orchestrator/deploy.sh and
#     the 'live' alias exists.
#   - Env vars AWS_REGION, AWS_ACCOUNTID are populated by the workshop
#     bootstrap.
set -euo pipefail

cd "$(dirname "$0")"

: "${AWS_REGION:?AWS_REGION must be set (should be pre-populated by the workshop bootstrap)}"
: "${AWS_ACCOUNTID:?AWS_ACCOUNTID must be set (should be pre-populated by the workshop bootstrap)}"

FUNCTION_NAME="${FUNCTION_NAME_CI:-ci-runner-orchestrator}"
REPO_NAME="${REPO_NAME:-lambda-mvm-workshop-code-review}"
# Module 3 uses its own branch so it never shares a trigger with Module 2's
# review lab (which uses feature/bad-code). A push to one branch must not fire
# the other module's orchestrator.
export BRANCH="${BRANCH:-feature/ci-pipeline}"
STATEMENT_ID="${STATEMENT_ID:-codecommit-ci-trigger}"
export TRIGGER_NAME="${TRIGGER_NAME:-ci-runner-trigger}"

export FUNCTION_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNTID}:function:${FUNCTION_NAME}:live"
REPO_ARN="arn:aws:codecommit:${AWS_REGION}:${AWS_ACCOUNTID}:${REPO_NAME}"

echo "Function ARN: ${FUNCTION_ARN}"
echo "Repo ARN:     ${REPO_ARN}"
echo ""

# add-permission is not idempotent - treat "statement already exists" as OK.
echo "==> granting CodeCommit permission to invoke the function"
if aws lambda add-permission \
  --function-name "${FUNCTION_NAME}" \
  --qualifier live \
  --statement-id "${STATEMENT_ID}" \
  --action lambda:InvokeFunction \
  --principal codecommit.amazonaws.com \
  --source-arn "${REPO_ARN}" \
  --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "    permission added"
else
  echo "    permission already present (skipping)"
fi

# Upsert our trigger into the repo's existing trigger set (matched by name)
# instead of replacing it, so this CI trigger coexists with Module 2's review
# trigger. put-repository-triggers overwrites the whole set, so a naive write
# here would silently delete Module 2's trigger. createReference is included
# alongside updateReference so the first push that *creates* feature/ci-pipeline
# also fires the runner.
echo "==> registering repository trigger (${TRIGGER_NAME} on ${BRANCH})"
EXISTING=$(aws codecommit get-repository-triggers \
  --repository-name "${REPO_NAME}" --region "${AWS_REGION}" \
  --query 'triggers' --output json 2>/dev/null || echo '[]')

python3 - "${EXISTING}" > /tmp/ci-repo-triggers.json <<'PY'
import json, os, sys
existing = json.loads(sys.argv[1] or "[]")
new = {
    "name": os.environ["TRIGGER_NAME"],
    "destinationArn": os.environ["FUNCTION_ARN"],
    "branches": [os.environ["BRANCH"]],
    "events": ["createReference", "updateReference"],
}
merged = [t for t in existing if t.get("name") != new["name"]] + [new]
json.dump({"triggers": merged}, sys.stdout)
PY

aws codecommit put-repository-triggers \
  --repository-name "${REPO_NAME}" \
  --cli-input-json file:///tmp/ci-repo-triggers.json \
  --region "${AWS_REGION}"

echo ""
echo "==> testing the trigger"
aws codecommit test-repository-triggers \
  --repository-name "${REPO_NAME}" \
  --triggers "[{\"name\":\"${TRIGGER_NAME}\",\"destinationArn\":\"${FUNCTION_ARN}\",\"branches\":[\"${BRANCH}\"],\"events\":[\"createReference\",\"updateReference\"]}]" \
  --region "${AWS_REGION}"

echo ""
echo "Trigger wired. A push to ${BRANCH} on ${REPO_NAME} will now invoke the CI runner orchestrator."
