"""Module 2 reviewer — Claude Code review with the callback pattern.

Uses Claude Code CLI (headless -p mode) for AI-powered code review
inside a Lambda MicroVM. Built for the durable-execution callback
flow: return 202 immediately, run the review in a background thread,
then call SendDurableExecutionCallbackSuccess (or ...Failure) to wake
the sleeping orchestrator.

Single HTTP server on port 9000. Path-based routing:
  POST /review     — accepts a review request with callback_id
  GET  /health     — health check
  ANY  /aws/...    — MicroVM lifecycle hooks (returns 200 OK)
  everything else  — 200 OK (treated as a hook)

The MicroVM lifecycle model this file supports:

  1. Lambda POSTs /aws/lambda-microvms/runtime/v1/run   → 200
  2. Orchestrator POSTs /review with {..., callback_id} → 202 (immediate)
  3. Background thread runs Claude on the PR's diff (30s to a few min)
  4. Background thread calls SendDurableExecutionCallbackSuccess with
     the review payload; the orchestrator wakes up.
  5. Orchestrator calls TerminateMicrovm; Lambda POSTs
     /aws/lambda-microvms/runtime/v1/terminate         → 200
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import boto3

logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","message":"%(message)s"}',
)
logger = logging.getLogger(__name__)

PORT = 9000


# ── Claude Code with Bedrock: no API key needed, uses IAM role credentials ────


def verify_bedrock_access() -> bool:
    """Verify that CLAUDE_CODE_USE_BEDROCK is set (credentials come from the execution role)."""
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1":
        logger.info("Claude Code configured to use Bedrock (IAM role credentials)")
        return True
    logger.warning("CLAUDE_CODE_USE_BEDROCK not set; Claude calls will fail")
    return False


# ── Claude review: clone, diff, run claude -p, post PR comment ────────────────


def run_review(body: dict) -> dict:
    repo_name = body["repo_name"]
    pr_id = str(body["pr_id"])
    source_commit = body["source_commit"]
    destination_commit = body["destination_commit"]
    region = body["region"]

    work_dir = tempfile.mkdtemp(prefix="review-")
    try:
        # git config for CodeCommit
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
            ["git", "clone", "--no-checkout", clone_url, work_dir],
            check=True, capture_output=True, text=True, timeout=120,
        )
        subprocess.run(
            ["git", "fetch", "--all"],
            cwd=work_dir, check=True, capture_output=True, text=True, timeout=60,
        )

        diff_res = subprocess.run(
            ["git", "diff", f"{destination_commit}..{source_commit}"],
            cwd=work_dir, capture_output=True, text=True, timeout=60,
        )
        diff = diff_res.stdout
        if not diff.strip():
            return {"success": True, "message": "Empty diff", "review": ""}

        logger.info(f"diff: {len(diff.splitlines())} lines; running claude")
        prompt = (
            "You are a senior software engineer performing a code review. "
            "Review the following git diff and provide structured feedback: "
            "bugs, security concerns, performance issues, code quality, "
            "and missing tests. Be concise. Markdown output.\n\n"
            "```diff\n" + diff + "\n```"
        )
        claude_env = {
            **os.environ,
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_MODEL": os.environ.get(
                "ANTHROPIC_MODEL", "us.anthropic.claude-sonnet-4-6"
            ),
            # Bedrock endpoint region: use the review's region so the
            # cross-region inference profile resolves in the deploy region.
            "AWS_REGION": region,
            "AWS_DEFAULT_REGION": region,
        }

        # Claude Code makes several Bedrock calls per run. A transient Bedrock
        # response (a 4xx/5xx on one of those calls) makes the CLI exit non-zero
        # even though the same review usually succeeds on a retry, so run the
        # whole invocation up to max_attempts times with exponential backoff.
        # A persistent client error (for example the model not being enabled for
        # the execution role) will still fail after the retries; last_error then
        # carries the reason so it shows up in the logs and the callback.
        max_attempts = int(os.environ.get("CLAUDE_MAX_ATTEMPTS", "3"))
        review_text = ""
        last_error = ""
        for attempt in range(1, max_attempts + 1):
            claude = subprocess.run(
                ["claude", "-p", "--output-format", "text", prompt],
                capture_output=True, text=True, timeout=300, env=claude_env,
            )
            if claude.returncode == 0 and claude.stdout.strip():
                review_text = claude.stdout.strip()
                break
            # claude -p often prints the failure to stdout rather than stderr,
            # so capture both to make the reason visible (the previous version
            # only logged stderr, which was empty on Bedrock client errors).
            last_error = (claude.stderr or claude.stdout or "").strip()[-800:] or "no output"
            logger.warning(
                f"claude attempt {attempt}/{max_attempts} failed "
                f"(exit {claude.returncode}): {last_error}"
            )
            if attempt < max_attempts:
                time.sleep(2 ** attempt)  # 2s, then 4s

        if not review_text:
            return {
                "success": False,
                "error": f"claude failed after {max_attempts} attempts: {last_error}",
            }

        comment = (
            "## 🤖 Claude AI Code Review (durable orchestration)\n\n"
            f"{review_text}\n\n"
            "---\n"
            "*Review generated by Claude Code headless inside a Lambda MicroVM, "
            "orchestrated by a durable function via the callback pattern.*"
        )
        subprocess.run(
            ["aws", "codecommit", "post-comment-for-pull-request",
             "--pull-request-id", pr_id,
             "--repository-name", repo_name,
             "--before-commit-id", destination_commit,
             "--after-commit-id", source_commit,
             "--content", comment,
             "--region", region],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return {"success": True, "message": "Review posted", "review_length": len(review_text)}

    except subprocess.TimeoutExpired as e:
        return {"success": False, "error": f"timeout: {e}"}
    except subprocess.CalledProcessError as e:
        return {"success": False, "error": f"command failed: {(e.stderr or '')[-500:]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ── Background runner: review + callback ──────────────────────────────────────


def run_review_and_callback(body: dict, callback_id: str) -> None:
    region = body["region"]
    lambda_client = boto3.client("lambda", region_name=region)
    try:
        result = run_review(body)
        if result.get("success"):
            lambda_client.send_durable_execution_callback_success(
                CallbackId=callback_id,
                Result=json.dumps(result).encode(),
            )
            logger.info(f"callback success sent for {callback_id[:24]}...")
        else:
            lambda_client.send_durable_execution_callback_failure(
                CallbackId=callback_id,
                Error={
                    "ErrorType": "ReviewFailed",
                    "ErrorMessage": result.get("error") or result.get("message") or "unknown",
                },
            )
            logger.info(f"callback failure sent for {callback_id[:24]}...")
    except Exception as e:
        logger.error(f"runner threw: {e!r}\n{traceback.format_exc()}")
        try:
            lambda_client.send_durable_execution_callback_failure(
                CallbackId=callback_id,
                Error={"ErrorType": type(e).__name__, "ErrorMessage": str(e)[:1024]},
            )
        except Exception as e2:
            logger.error(f"failure-callback ALSO failed: {e2!r}")


# ── HTTP handler ──────────────────────────────────────────────────────────────


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
        if self.path == "/health":
            self._json(200, {"status": "healthy", "bedrock_enabled": os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1"})
        else:
            self._json(200, {"status": "ok"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""

        if self.path == "/review":
            if not verify_bedrock_access():
                self._json(500, {"error": "CLAUDE_CODE_USE_BEDROCK not configured"})
                return

            try:
                body = json.loads(raw or b"{}")
            except Exception as e:
                self._json(400, {"error": f"invalid JSON: {e}"})
                return

            required = ["repo_name", "pr_id", "source_commit",
                        "destination_commit", "region"]
            missing = [f for f in required if f not in body]
            if missing:
                self._json(400, {"error": f"missing fields: {missing}"})
                return

            callback_id = body.get("callback_id")
            if not callback_id:
                self._json(400, {"error": "callback_id is required"})
                return

            # 202 first, then run in background.
            self._json(202, {"status": "accepted", "callback_id": callback_id})
            threading.Thread(
                target=run_review_and_callback,
                args=(body, callback_id),
                daemon=True,
            ).start()
            return

        # Any other POST (lifecycle hooks) — always 200.
        self._json(200, {"status": "ok"})


class ThreadedHTTP(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    verify_bedrock_access()  # log status at boot
    server = ThreadedHTTP(("0.0.0.0", PORT), Handler)
    logger.info(f"Module 2 reviewer listening on 0.0.0.0:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
