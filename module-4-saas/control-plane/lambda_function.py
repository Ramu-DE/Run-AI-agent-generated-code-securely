"""Multi-tenant SaaS control plane for the Module 4 lab.

Deployed as a Lambda function behind an API Gateway HTTP API, so it is a real
always-on service rather than a process in the code editor. It is the single
entry point in front of many tenants. For each request it:

  1. resolves the tenant from the X-Tenant header
  2. launches that tenant's own MicroVM on first use and caches it warm,
     reusing it for later requests (the MicroVM auto-resumes if it suspended)
  3. mints a short-lived auth token scoped to the tenant app's port
  4. reverse-proxies the request to the tenant's MicroVM and returns the response

Every tenant gets its own MicroVM launched from the same image, so tenants
are isolated at the hardware boundary (a "silo" per tenant) while capacity is
created on demand and costs nothing between requests (the economics of a
"pool"). The control plane never terminates a MicroVM; each one self-terminates
through its idle policy.

The per-tenant cache lives in module scope, so it is shared across requests
that reuse the same warm Lambda execution environment. This mirrors the
single-process control plane pattern: state is local to one running instance.
"""

import base64
import json
import os
import threading
import time
import urllib.request
import urllib.error

import boto3

REGION = os.environ["MVM_REGION"]
IMAGE_ARN = os.environ["MVM_IMAGE_ARN"]
EXECUTION_ROLE = os.environ["MVM_EXECUTION_ROLE_ARN"]

# The port the tenant app listens on inside its MicroVM. Auth tokens are
# scoped to it, and the proxy targets it with the X-aws-proxy-port header.
TENANT_PORT = 8080
AUTH_TOKEN_TTL_MIN = 60
AUTH_REFRESH_SKEW_SEC = 5 * 60
RUNNING_TIMEOUT_SEC = 30
POLL_INTERVAL_SEC = 2

microvms = boto3.client("lambda-microvms", region_name=REGION)

# tenant -> {"microvm_id", "endpoint", "token", "token_expires"}
_cache = {}
_locks = {}
_locks_guard = threading.Lock()


def _tenant_lock(tenant):
    with _locks_guard:
        if tenant not in _locks:
            _locks[tenant] = threading.Lock()
        return _locks[tenant]


def launch_tenant_vm(tenant):
    """Launch a MicroVM for the tenant and wait until it is RUNNING."""
    print(f"launching MicroVM for tenant={tenant}")
    run = microvms.run_microvm(
        imageIdentifier=IMAGE_ARN,
        imageVersion="1.0",
        ingressNetworkConnectors=[
            f"arn:aws:lambda:{REGION}:aws:network-connector:"
            f"aws-network-connector:HTTP_INGRESS"
        ],
        egressNetworkConnectors=[
            f"arn:aws:lambda:{REGION}:aws:network-connector:"
            f"aws-network-connector:INTERNET_EGRESS"
        ],
        executionRoleArn=EXECUTION_ROLE,
        # autoResume keeps the tenant's VM warm: it suspends when idle and
        # resumes on the next request, then self-terminates once suspended
        # long enough. The control plane never terminates it explicitly.
        idlePolicy={
            "maxIdleDurationSeconds": 900,
            "suspendedDurationSeconds": 300,
            "autoResumeEnabled": True,
        },
    )
    microvm_id = run["microvmId"]
    endpoint = run.get("endpoint")
    state = run.get("state")

    deadline = time.time() + RUNNING_TIMEOUT_SEC
    while state != "RUNNING":
        if state in ("TERMINATING", "TERMINATED"):
            raise RuntimeError(f"microvm {microvm_id} entered {state} before RUNNING")
        if time.time() > deadline:
            raise TimeoutError(f"microvm {microvm_id} not RUNNING after {RUNNING_TIMEOUT_SEC}s")
        time.sleep(POLL_INTERVAL_SEC)
        got = microvms.get_microvm(microvmIdentifier=microvm_id)
        state = got["state"]
        endpoint = got.get("endpoint", endpoint)

    print(f"tenant={tenant} microvm={microvm_id} RUNNING")
    return microvm_id, endpoint


def mint_token(microvm_id):
    out = microvms.create_microvm_auth_token(
        microvmIdentifier=microvm_id,
        allowedPorts=[{"port": TENANT_PORT}],
        expirationInMinutes=AUTH_TOKEN_TTL_MIN,
    )
    return out["authToken"]["X-aws-proxy-auth"], time.time() + AUTH_TOKEN_TTL_MIN * 60


def resolve_upstream(tenant):
    """Return (microvm_id, endpoint, token) for the tenant, launching its
    MicroVM on first use and refreshing the auth token before it expires."""
    with _tenant_lock(tenant):
        entry = _cache.get(tenant)
        if entry is None:
            microvm_id, endpoint = launch_tenant_vm(tenant)
            entry = {"microvm_id": microvm_id, "endpoint": endpoint,
                     "token": None, "token_expires": 0}
            _cache[tenant] = entry
        if not entry["token"] or time.time() > entry["token_expires"] - AUTH_REFRESH_SKEW_SEC:
            entry["token"], entry["token_expires"] = mint_token(entry["microvm_id"])
        return entry["microvm_id"], entry["endpoint"], entry["token"]


def proxy(method, tenant, microvm_id, path, query, body, endpoint, token):
    url = f"https://{endpoint}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(
        url,
        method=method,
        data=body if body else None,
        headers={
            "X-aws-proxy-auth": token,
            "X-aws-proxy-port": str(TENANT_PORT),
            "X-Tenant": tenant,
            # Stamp the request with the tenant's MicroVM id, so the app can
            # report which isolated instance served it.
            "X-Microvm-Id": microvm_id,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read(), r.headers.get("Content-Type", "application/json")


def _response(status, body, content_type="application/json"):
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    return {"statusCode": status, "headers": {"Content-Type": content_type}, "body": body}


def lambda_handler(event, context):
    # API Gateway HTTP API, payload format 2.0.
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "/")
    query = event.get("rawQueryString", "")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    body = event.get("body") or b""
    if isinstance(body, str):
        body = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()

    # Health check that launches nothing.
    if path == "/health":
        return _response(200, json.dumps({"status": "ok"}))

    tenant = headers.get("x-tenant")
    if not tenant:
        return _response(400, json.dumps({"error": "missing X-Tenant header"}))

    try:
        microvm_id, endpoint, token = resolve_upstream(tenant)
        status, payload, ctype = proxy(method, tenant, microvm_id, path, query, body, endpoint, token)
        return _response(status, payload, ctype)
    except urllib.error.HTTPError as e:
        return _response(e.code, e.read(), e.headers.get("Content-Type", "application/json"))
    except Exception as e:
        print(f"error for tenant={tenant}: {e!r}")
        # Drop the cache entry so the next request relaunches a fresh VM.
        _cache.pop(tenant, None)
        return _response(502, json.dumps({"error": "tenant MicroVM unavailable"}))
