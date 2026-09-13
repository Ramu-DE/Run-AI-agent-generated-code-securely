# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
app.py — Sandboxed Code Execution Service for Lambda MicroVM

Runs two HTTP servers:
- Port 8080: Code execution API (application traffic)
- Port 9000: Lifecycle hooks (MicroVM runtime callbacks)
"""

import os
import json
import logging
import subprocess
import tempfile
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}'
)
logger = logging.getLogger(__name__)

MICROVM_ID = None  # Set on /launch hook


# ── Application handler (port 8080) ──────────────────────────────────────────

class AppHandler(BaseHTTPRequestHandler):
    """Handles code execution requests from AI agents."""

    def log_message(self, format, *args):
        logger.info(f"APP {format % args}")

    def send_json(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/":
            self.send_json(200, {
                "service": "code-execution-sandbox",
                "microvm_id": MICROVM_ID,
                "status": "ready"
            })
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/execute":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            code = body.get("code", "")
            if not code.strip():
                self.send_json(400, {"error": "code field is required"})
                return
            self.send_json(200, execute_code(code))
        else:
            self.send_json(404, {"error": "not found"})


def execute_code(code: str) -> dict:
    """Execute Python code in an isolated subprocess with a 30-second timeout."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        script_path = f.name
    try:
        result = subprocess.run(
            ["python3", script_path],
            capture_output=True, text=True, timeout=30,
            cwd=tempfile.gettempdir()
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "success": result.returncode == 0
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Execution timed out after 30 seconds",
                "exit_code": -1, "success": False}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1, "success": False}
    finally:
        os.unlink(script_path)


# ── Lifecycle hook handler (port 9000) ───────────────────────────────────────

class HookHandler(BaseHTTPRequestHandler):
    """Handles MicroVM lifecycle hook callbacks from the runtime."""

    _raw_body = b""

    def log_message(self, format, *args):
        logger.info(f"HOOK {format % args}")

    def send_json(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        self._raw_body = raw
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            logger.warning(f"hook body was not valid JSON: {raw[:200]!r}")
            return {}

    def do_POST(self):
        global MICROVM_ID
        body = self.read_body()

        if self.path.endswith("/health"):
            # Called during image build — snapshot is taken after this returns 200.
            # Your application must be fully started before this hook fires.
            logger.info("/health — application is up and running")
            self.send_json(200, {"status": "I'm here!"})
        
        elif self.path.endswith("/ready"):
            # Called during image build — snapshot is taken after this returns 200.
            # Your application must be fully started before this hook fires.
            logger.info("/ready — application is up, snapshot will be taken")
            self.send_json(200, {"status": "ready"})

        elif self.path.endswith("/run") or self.path.endswith("/launch"):
            # Called after the VM is launched from the snapshot.
            # The MicroVM runtime posts to .../runtime/v1/run — "/launch" is
            # kept only as an alias for older docs. Returning anything other
            # than 200 here makes Lambda terminate the VM immediately with
            # "Run lifecycle hook returned HTTP status <code>".
            #
            # The run payload does not reliably carry the MicroVM id, so try
            # the documented key, then common variants, then the header the
            # proxy adds. Log the raw body so the real contract is visible.
            logger.info(f"/run raw body={self._raw_body[:300]!r}")
            MICROVM_ID = (
                body.get("microVmId")
                or body.get("microvmId")
                or body.get("microVmID")
                or self.headers.get("X-Amz-Microvm-Id")
                or os.environ.get("AWS_LAMBDA_MICROVM_ID")
            )
            logger.info(
                f"/run — microVmId={MICROVM_ID} "
                f"image={os.environ.get('AWS_LAMBDA_MICROVM_IMAGE_NAME')} "
                f"version={os.environ.get('AWS_LAMBDA_MICROVM_IMAGE_VERSION')}"
            )
            self.send_json(200, {"status": "launched", "microvm_id": MICROVM_ID})

        elif self.path.endswith("/suspend"):
            # Called before VM is suspended.
            # Close open connections and flush any pending state.
            logger.info("/suspend — flushing state before suspend")
            self.send_json(200, {"status": "suspending"})

        elif self.path.endswith("/resume"):
            # Called after VM resumes from suspend.
            # Re-establish connections cleaned up before suspend.
            logger.info("/resume — re-establishing connections")
            self.send_json(200, {"status": "resumed"})

        elif self.path.endswith("/terminate"):
            # Called before VM is terminated.
            # Flush pending logs or metrics.
            logger.info("/terminate — flushing before shutdown")
            self.send_json(200, {"status": "terminating"})

        else:
            # Unknown hook: respond 200 on purpose. Lambda terminates the
            # MicroVM if a lifecycle hook returns a non-200 status, so an
            # unrecognised (or newly added) hook must not fail the VM.
            logger.warning(f"unknown hook {self.path} — returning 200")
            self.send_json(200, {"status": "ok", "hook": self.path})


# ── Startup ───────────────────────────────────────────────────────────────────

def start_hook_server():
    """Start the lifecycle hook server on port 9000."""
    server = HTTPServer(("0.0.0.0", 9000), HookHandler)
    logger.info("Lifecycle hook server listening on port 9000")
    server.serve_forever()


def main():
    # Start lifecycle hook server in background thread
    threading.Thread(target=start_hook_server, daemon=True).start()

    # Start application server on port 8080
    server = HTTPServer(("0.0.0.0", 8080), AppHandler)
    logger.info("Code execution API listening on port 8080")
    server.serve_forever()


if __name__ == "__main__":
    main()