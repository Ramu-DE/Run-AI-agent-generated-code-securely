"""Per-tenant application for the Module 4 SaaS lab.

Runs inside a Lambda MicroVM. Every tenant gets its own MicroVM launched from
this same image, so tenants are isolated at the hardware boundary while sharing
one application build.

It also demonstrates *data* isolation with the token vending machine pattern.
For each request the app takes the tenant id the control plane forwards in the
X-Tenant header, hydrates an S3 folder-per-tenant policy template with that
tenant, and calls sts:AssumeRole passing that policy as a *session policy*. The
resulting credentials are the intersection of the tenant-access role's broad
bucket policy and the scoped session policy, so they can only reach
s3://<bucket>/<tenant>/*. One role and one policy template isolate every tenant
with no per-tenant IAM.

Two HTTP servers:
  - the app on port 8080: /, /api/info, /api/files[/<name>], /health
  - the lifecycle hooks on port 9000: the MicroVM 'ready' hook
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import boto3
from botocore.exceptions import ClientError

APP_PORT = 8080
HOOKS_PORT = 9000

# The Lambda MicroVM runtime provides credentials for its execution role, and
# usually exports the region. Fall back to us-east-1 (the workshop's default).
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"

_ready = threading.Event()
_sts = boto3.client("sts", region_name=REGION)

# Bucket name and tenant-access role follow fixed conventions from the workshop
# stack, so the app resolves them from its own account id rather than needing
# them baked in. Resolved once, lazily, on the first request that needs S3.
_account = {"id": None}
_account_lock = threading.Lock()


def _account_id():
    if _account["id"] is None:
        with _account_lock:
            if _account["id"] is None:
                _account["id"] = _sts.get_caller_identity()["Account"]
    return _account["id"]


def _bucket():
    return f"lambda-mvm-workshop-tenant-data-{_account_id()}"


def _tenant_access_role_arn():
    return f"arn:aws:iam::{_account_id()}:role/TenantAccessRole-workshop"


def scoped_policy(bucket, tenant):
    """Folder-per-tenant S3 template hydrated for one tenant.

    ListBucket is limited to the tenant's prefix, and object access is limited
    to objects under <tenant>/. Passed to sts:AssumeRole as the session policy;
    the effective permission is the intersection of this and the tenant-access
    role's broad bucket policy.
    """
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {"StringLike": {"s3:prefix": [f"{tenant}/", f"{tenant}/*"]}},
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": f"arn:aws:s3:::{bucket}/{tenant}/*",
            },
        ],
    })


def scoped_s3(tenant):
    """Assume the tenant-access role with a session policy scoped to this tenant
    and return an S3 client whose credentials can only reach the tenant's
    prefix."""
    creds = _sts.assume_role(
        RoleArn=_tenant_access_role_arn(),
        RoleSessionName=f"tenant-{tenant}"[:64],
        Policy=scoped_policy(_bucket(), tenant),
        DurationSeconds=900,
    )["Credentials"]
    return boto3.client(
        "s3",
        region_name=REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


PAGE = """<!doctype html>
<html>
  <head><title>Tenant app on a Lambda MicroVM</title></head>
  <body style="font-family: sans-serif; max-width: 40rem; margin: 3rem auto;">
    <h1>Hello from a Lambda MicroVM</h1>
    <p>This page is served by a per-tenant MicroVM behind the SaaS control plane.</p>
    <p>Fetch <a href="/api/info">/api/info</a> to see which tenant and which
       MicroVM served the request, or <a href="/api/files">/api/files</a> to
       list this tenant's objects in the shared bucket.</p>
  </body>
</html>
"""


class AppHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, content_type):
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, indent=2), "application/json")

    def _tenant(self):
        return self.headers.get("X-Tenant", "unknown")

    def _query(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _s3_error(self, tenant, target, e):
        # Surface the S3 error (for example AccessDenied) so cross-tenant
        # attempts show up as a clear, readable denial.
        self._json(403, {
            "tenant": tenant,
            "target_prefix": f"{target}/",
            "error": e.response["Error"]["Code"],
            "message": e.response["Error"]["Message"],
        })

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        tenant = self._tenant()
        if path == "/health":
            self._send(200, "OK", "text/plain")
        elif path == "/api/info":
            self._json(200, {
                "message": "Hello from a Lambda MicroVM",
                "tenant": tenant,
                "microvm": self.headers.get("X-Microvm-Id", "unknown"),
                "python": os.sys.version.split()[0],
            })
        elif path == "/api/files":
            # Credentials are scoped to X-Tenant. The optional ?tenant= chooses
            # which prefix to try, so pointing it at another tenant proves the
            # scoped credentials cannot cross the boundary.
            target = self._query().get("tenant", tenant)
            try:
                s3 = scoped_s3(tenant)
                resp = s3.list_objects_v2(Bucket=_bucket(), Prefix=f"{target}/")
                keys = [o["Key"] for o in resp.get("Contents", [])]
                self._json(200, {
                    "tenant": tenant,
                    "target_prefix": f"{target}/",
                    "bucket": _bucket(),
                    "keys": keys,
                })
            except ClientError as e:
                self._s3_error(tenant, target, e)
        elif path.startswith("/api/files/"):
            name = path[len("/api/files/"):]
            target = self._query().get("tenant", tenant)
            try:
                s3 = scoped_s3(tenant)
                obj = s3.get_object(Bucket=_bucket(), Key=f"{target}/{name}")
                self._send(200, obj["Body"].read(), "text/plain")
            except ClientError as e:
                self._s3_error(tenant, target, e)
        else:
            self._send(200, PAGE, "text/html")

    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        tenant = self._tenant()
        if path.startswith("/api/files/"):
            name = path[len("/api/files/"):]
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            try:
                s3 = scoped_s3(tenant)
                s3.put_object(Bucket=_bucket(), Key=f"{tenant}/{name}", Body=body)
                self._json(200, {"tenant": tenant, "wrote": f"{tenant}/{name}", "bytes": len(body)})
            except ClientError as e:
                self._s3_error(tenant, tenant, e)
        else:
            self._send(404, "not found", "text/plain")


class HooksHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path == "/aws/lambda-microvms/runtime/v1/ready":
            self.send_response(200 if _ready.is_set() else 503)
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()


def serve(port, handler):
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()


def main():
    threading.Thread(target=serve, args=(HOOKS_PORT, HooksHandler), daemon=True).start()
    # The app is ready to serve as soon as its listener is up.
    server = ThreadingHTTPServer(("0.0.0.0", APP_PORT), AppHandler)
    _ready.set()
    print(f"tenant app listening on :{APP_PORT}, hooks on :{HOOKS_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
