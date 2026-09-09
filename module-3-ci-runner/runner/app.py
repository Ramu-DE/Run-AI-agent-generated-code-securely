"""Ephemeral CI runner agent for the Module 3 lab.

Runs inside a Lambda MicroVM. A single HTTP server on port 9000 with
path-based routing:
  POST /run    - accepts a CI job {repo_name, branch, pr_id, source_commit,
                 destination_commit, region}, returns 202 immediately, and
                 runs the pipeline on a background thread
  GET  /health - health check
  ANY  else    - 200 OK (treated as a MicroVM lifecycle hook)

The lifecycle this file supports:
  1. Lambda POSTs the run lifecycle hook            -> 200
  2. Orchestrator POSTs /run with the job           -> 202 (immediate)
  3. Background thread clones the repo, checks out the pushed commit,
     runs ci/steps.sh (or a default check), and posts the pass/fail
     result as a comment on the pull request
  4. The MicroVM goes idle and self-terminates via its idle policy
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

PORT = 9000
REQUIRED = ["repo_name", "branch", "pr_id", "source_commit", "destination_commit", "region"]


def run_pipeline(job: dict) -> dict:
    """Clone the repo at the pushed commit and run its pipeline."""
    repo_name = job["repo_name"]
    source_commit = job["source_commit"]
    region = job["region"]
    work = tempfile.mkdtemp(prefix="ci-")
    try:
        # Authenticate git to CodeCommit using the MicroVM execution role.
        subprocess.run(
            ["git", "config", "--global", "credential.helper",
             "!aws codecommit credential-helper $@"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "--global", "credential.UseHttpPath", "true"],
            check=True, capture_output=True, text=True,
        )

        clone_url = f"https://git-codecommit.{region}.amazonaws.com/v1/repos/{repo_name}"
        logger.info(f"cloning {repo_name}")
        subprocess.run(
            ["git", "clone", clone_url, work],
            check=True, capture_output=True, text=True, timeout=120,
        )
        subprocess.run(
            ["git", "checkout", source_commit],
            cwd=work, check=True, capture_output=True, text=True, timeout=60,
        )

        # Run the pipeline defined in the repo, or a default if none exists.
        if os.path.exists(os.path.join(work, "ci", "steps.sh")):
            logger.info("running ci/steps.sh")
            cmd = ["bash", "ci/steps.sh"]
        else:
            logger.info("no ci/steps.sh found, running default checks")
            cmd = ["bash", "-c",
                   "python -m compileall -q . && "
                   "if [ -f requirements.txt ]; then pip install -q -r requirements.txt; fi"]

        proc = subprocess.run(
            cmd, cwd=work, capture_output=True, text=True, timeout=600,
        )
        output = (proc.stdout + proc.stderr)[-4000:]
        return {"passed": proc.returncode == 0, "exit_code": proc.returncode, "output": output}
    except subprocess.TimeoutExpired as e:
        return {"passed": False, "exit_code": 124, "output": f"pipeline timed out: {e}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def post_result(job: dict, result: dict) -> None:
    """Post the build result as a comment on the pull request."""
    status = "passed" if result["passed"] else "failed"
    comment = (
        f"## CI build {status} (exit code {result['exit_code']})\n\n"
        f"Ran on an ephemeral Lambda MicroVM runner, commit `{job['source_commit'][:10]}`.\n\n"
        "Build output (last 4000 chars):\n\n"
        "```\n" + result["output"].strip() + "\n```\n"
    )
    subprocess.run(
        ["aws", "codecommit", "post-comment-for-pull-request",
         "--pull-request-id", str(job["pr_id"]),
         "--repository-name", job["repo_name"],
         "--before-commit-id", job["destination_commit"],
         "--after-commit-id", job["source_commit"],
         "--content", comment,
         "--region", job["region"]],
        check=True, capture_output=True, text=True, timeout=30,
    )
    logger.info(f"posted {status} result to PR {job['pr_id']}")


def run_job(job: dict) -> None:
    try:
        result = run_pipeline(job)
    except Exception as e:
        result = {"passed": False, "exit_code": -1, "output": repr(e)}
    try:
        post_result(job, result)
    except Exception as e:
        logger.error(f"failed to post result: {e!r}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info(f"HTTP {self.address_string()} {fmt % args}")

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._json(200, {"status": "ok"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""

        if self.path == "/run":
            try:
                job = json.loads(raw or b"{}")
            except Exception as e:
                self._json(400, {"error": f"invalid JSON: {e}"})
                return
            missing = [f for f in REQUIRED if f not in job]
            if missing:
                self._json(400, {"error": f"missing fields: {missing}"})
                return

            # 202 first, then run the job on a background thread.
            self._json(202, {"status": "accepted"})
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            return

        # Any other POST (MicroVM lifecycle hooks) - always 200.
        self._json(200, {"status": "ok"})


class ThreadedHTTP(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    server = ThreadedHTTP(("0.0.0.0", PORT), Handler)
    logger.info(f"CI runner listening on 0.0.0.0:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
