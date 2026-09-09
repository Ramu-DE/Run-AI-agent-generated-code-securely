#!/usr/bin/env bash
# Stages a CI pipeline and sample code in the workshop CodeCommit repo so a
# push fires the runner. Mirrors Module 2's prepare-review.sh: it configures
# git for CodeCommit, clones the repo, checks out the branch, and writes the
# files, but stops short of committing so you drive the push yourself.
set -euo pipefail

: "${AWS_REGION:?AWS_REGION must be set (should be pre-populated by the workshop bootstrap)}"

REPO_NAME="${REPO_NAME:-lambda-mvm-workshop-code-review}"
BRANCH="${BRANCH:-feature/ci-pipeline}"
WORK_DIR="${WORK_DIR:-/tmp/ci-demo}"

echo "==> configuring git credential helper for CodeCommit"
git config --global credential.helper '!aws codecommit credential-helper $@'
git config --global credential.UseHttpPath true

echo "==> cloning ${REPO_NAME} and checking out ${BRANCH}"
rm -rf "${WORK_DIR}"
git clone "https://git-codecommit.${AWS_REGION}.amazonaws.com/v1/repos/${REPO_NAME}" "${WORK_DIR}"
cd "${WORK_DIR}"
# feature/ci-pipeline is Module 3's own branch and may not exist yet, so create
# it on first run. Pushing a brand-new branch fires the CI trigger via the
# createReference event registered by wire-codecommit.sh.
git checkout "${BRANCH}" 2>/dev/null || git checkout -b "${BRANCH}"

echo "==> writing ci/steps.sh, src/calculator.py, tests/test_calculator.py"
mkdir -p ci src tests

cat > ci/steps.sh <<'EOF'
#!/usr/bin/env bash
set -e

echo "== compile =="
python -m compileall -q src

echo "== test =="
python -m unittest discover -s tests -p 'test_*.py'
EOF

cat > src/calculator.py <<'EOF'
def add(a, b):
    return a + b


def divide(a, b):
    return a / b
EOF

cat > tests/test_calculator.py <<'EOF'
import unittest

from src.calculator import add


class TestCalculator(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
EOF

echo ""
echo "Repo ready at ${WORK_DIR} on branch ${BRANCH}, with the pipeline and sample code written for you to commit."
echo "Next, run these git commands yourself to fire the runner:"
echo ""
echo "  cd ${WORK_DIR}"
echo "  git add ci src tests"
echo "  git commit -m 'Add CI pipeline and calculator module'"
echo "  git push origin ${BRANCH}"
