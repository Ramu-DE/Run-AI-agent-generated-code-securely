#!/usr/bin/env bash
# Prepares a code-review commit for the durable-orchestration lab.
# Points git at CodeCommit using the IDE's IAM role (no stored passwords),
# clones the review repo, checks out feature/bad-code, and drops in a small
# module with a few deliberate security problems. It stops short of committing:
# you run the git add / commit / push commands yourself to fire the trigger.
#
# Assumes:
#   - AWS_REGION is populated by the workshop bootstrap.
#   - The IDE's role has CodeCommit access (AWSCodeCommitFullAccess).
set -euo pipefail

: "${AWS_REGION:?AWS_REGION must be set (should be pre-populated by the workshop bootstrap)}"

REPO_NAME="${REPO_NAME:-lambda-mvm-workshop-code-review}"
BRANCH="${BRANCH:-feature/bad-code}"
WORKDIR="${WORKDIR:-/tmp/code-review}"
FILE_PATH="${FILE_PATH:-src/user_service.py}"

# 1) Authenticate git to CodeCommit using the IDE's IAM role (no stored passwords).
echo "==> configuring git credential helper for CodeCommit"
git config --global credential.helper '!aws codecommit credential-helper $@'
git config --global credential.UseHttpPath true
git config --global --get user.name  >/dev/null 2>&1 || git config --global user.name  "workshop-participant"
git config --global --get user.email >/dev/null 2>&1 || git config --global user.email "participant@example.com"

# 2) Resolve the repo's HTTPS clone URL.
CLONE_URL=$(aws codecommit get-repository \
  --repository-name "${REPO_NAME}" \
  --region "${AWS_REGION}" \
  --query "repositoryMetadata.cloneUrlHttp" --output text)
echo "    clone URL: ${CLONE_URL}"

# 3) Clone a fresh copy and switch to the target branch.
echo "==> cloning ${REPO_NAME} and checking out ${BRANCH}"
rm -rf "${WORKDIR}"
git clone --quiet "${CLONE_URL}" "${WORKDIR}"
cd "${WORKDIR}"
git checkout "${BRANCH}"

# 4) Add a small module with a few deliberate security problems.
echo "==> writing ${FILE_PATH}"
mkdir -p "$(dirname "${FILE_PATH}")"
cat > "${FILE_PATH}" <<'PY'
import sqlite3
import subprocess

# Credentials for the reporting database (FIXME before release)
DB_PASSWORD = "P@ssw0rd-2026"
API_TOKEN = "EXAMPLE-reporting-api-token-rotate-before-release"


def get_user(user_id):
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = '%s'" % user_id)
    return cursor.fetchone()


def export_report(report_name):
    cmd = "python /opt/reports/" + report_name
    return subprocess.call(cmd, shell=True)


def evaluate(expression):
    return eval(expression)
PY
# vary a trailing marker so re-runs always produce a new commit that fires the trigger
echo "# review requested at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "${FILE_PATH}"

echo ""
echo "Repo ready at ${WORKDIR} on branch ${BRANCH}, with ${FILE_PATH} written for you to commit."
echo "Next, run these git commands yourself to fire the review:"
echo ""
echo "  cd ${WORKDIR}"
echo "  git add ${FILE_PATH}"
echo "  git commit -m \"Add user lookup and reporting helpers\""
echo "  git push origin ${BRANCH}"
