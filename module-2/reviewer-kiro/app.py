"""Kiro reviewer — code review with the durable-execution callback pattern.

Uses the Kiro CLI (headless) for AI code review inside a Lambda MicroVM.
Built for the durable-execution callback flow: return 202 immediately, run
the review in a background thread, then call SendDurableExecutionCallbackSuccess
(or ...Failure) to wake the sleeping orchestrator. The Kiro API key is fetched
from Secrets Manager at runtime (never baked into the image).

Single HTTP server on port 9000. Path-based routing:
  POST /review     — accepts a review request with callback_id
  GET  /health     — health check
  ANY  /aws/...    — MicroVM lifecycle hooks (returns 200 OK)
  everything else  — 200 OK (treated as a hook)

The MicroVM lifecycle model this file supports:

  1. Lambda POSTs /aws/lambda-microvms/runtime/v1/run   → 200
  2. Orchestrator POSTs /review with {..., callback_id} → 202 (immediate)
  3. Background thread runs Kiro on the PR's diff (30s to a few min)
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
KIRO_API_KEY: str = ""  # populated at startup + refreshed on each request


# ── Secrets Manager: pull the Kiro API key ────────────────────────────────────


def fetch_kiro_api_key() -> str:
    secret_arn = os.environ.get("KIRO_API_KEY_SECRET_ARN", "")
    if not secret_arn:
        logger.warning("KIRO_API_KEY_SECRET_ARN not set; Kiro calls will fail")
        return ""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    try:
        client = boto3.client("secretsmanager", region_name=region)
        value = client.get_secret_value(SecretId=secret_arn)["SecretString"]
        logger.info(f"Kiro API key loaded (len={len(value)})")
        return value
    except Exception as e:
        logger.error(f"Failed to fetch Kiro API key: {e!r}")
        return ""


# ── Kiro review: clone, diff, run kiro-cli, post PR comment ───────────────────


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

        logger.info(f"diff: {len(diff.splitlines())} lines; running kiro")
        prompt = (
            "You are a senior software engineer performing a code review. "
            "Review the following git diff and provide structured feedback: "
            "bugs, security concerns, performance issues, code quality, "
            "and missing tests. Be concise. Markdown output.\n\n"
            "```diff\n" + diff + "\n```"
        )
        kiro = subprocess.run(
            ["kiro-cli", "chat", "--no-interactive", "--trust-tools=read,grep", prompt],
            capture_output=True, text=True, timeout=300,
            env={**os.environ, "KIRO_API_KEY": KIRO_API_KEY},
        )
        if kiro.returncode != 0:
            return {"success": False, "error": f"kiro-cli exit {kiro.returncode}: {kiro.stderr[-500:]}"}

        review_text = kiro.stdout.strip() or kiro.stderr.strip()
        if not review_text:
            return {"success": False, "error": "kiro-cli returned empty output"}

        comment = (
            "## 🤖 Kiro AI Code Review (durable orchestration)\n\n"
            f"{review_text}\n\n"
            "---\n"
            "*Review generated by Kiro headless inside a Lambda MicroVM, "
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
            self._json(200, {"status": "healthy", "kiro_key_loaded": bool(KIRO_API_KEY)})
        else:
            self._json(200, {"status": "ok"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""

        if self.path == "/review":
            global KIRO_API_KEY
            KIRO_API_KEY = fetch_kiro_api_key()
            if not KIRO_API_KEY:
                self._json(500, {"error": "Kiro API key not available"})
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
    global KIRO_API_KEY
    KIRO_API_KEY = fetch_kiro_api_key()  # best effort at boot
    server = ThreadedHTTP(("0.0.0.0", PORT), Handler)
    logger.info(f"Module 2 reviewer listening on 0.0.0.0:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
