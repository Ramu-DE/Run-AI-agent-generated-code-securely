#!/usr/bin/env bash
# One-command CodeCommit trigger wire-up.
# Replaces Step 7 of Module 2's durable-orchestration lab.
#
# Assumes:
#   - The orchestrator has been deployed via orchestrator/deploy.sh and
#     the 'live' alias exists.
#   - Env vars AWS_REGION, AWS_ACCOUNTID, FUNCTION_NAME are populated by
#     the workshop bootstrap.
set -euo pipefail

cd "$(dirname "$0")"

: "${AWS_REGION:?AWS_REGION must be set (should be pre-populated by the workshop bootstrap)}"
: "${AWS_ACCOUNTID:?AWS_ACCOUNTID must be set (should be pre-populated by the workshop bootstrap)}"
: "${FUNCTION_NAME:?FUNCTION_NAME must be set (should be pre-populated by the workshop bootstrap)}"

REPO_NAME="${REPO_NAME:-lambda-mvm-workshop-code-review}"
export BRANCH="${BRANCH:-feature/bad-code}"
STATEMENT_ID="${STATEMENT_ID:-codecommit-trigger}"
export TRIGGER_NAME="${TRIGGER_NAME:-ai-review-trigger}"

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
# instead of replacing it, so the Module 2 review trigger coexists with
# Module 3's CI trigger. put-repository-triggers overwrites the whole set, so
# a naive write here would silently delete Module 3's trigger.
echo "==> registering repository trigger (${TRIGGER_NAME} on ${BRANCH})"
EXISTING=$(aws codecommit get-repository-triggers \
  --repository-name "${REPO_NAME}" --region "${AWS_REGION}" \
  --query 'triggers' --output json 2>/dev/null || echo '[]')

python3 - "${EXISTING}" > /tmp/repo-triggers.json <<'PY'
import json, os, sys
existing = json.loads(sys.argv[1] or "[]")
new = {
    "name": os.environ["TRIGGER_NAME"],
    "destinationArn": os.environ["FUNCTION_ARN"],
    "branches": [os.environ["BRANCH"]],
    "events": ["updateReference"],
}
merged = [t for t in existing if t.get("name") != new["name"]] + [new]
json.dump({"triggers": merged}, sys.stdout)
PY

aws codecommit put-repository-triggers \
  --repository-name "${REPO_NAME}" \
  --cli-input-json file:///tmp/repo-triggers.json \
  --region "${AWS_REGION}"

echo ""
echo "==> testing the trigger"
aws codecommit test-repository-triggers \
  --repository-name "${REPO_NAME}" \
  --triggers "name=${TRIGGER_NAME},destinationArn=${FUNCTION_ARN},branches=${BRANCH},events=updateReference" \
  --region "${AWS_REGION}"

echo ""
echo "Trigger wired. A push to ${BRANCH} on ${REPO_NAME} will now invoke the durable orchestrator."
