# Troubleshooting log

What actually broke, how it was diagnosed, and what fixed it. Written from the real
session, including the false leads — those cost the most time.

---

## Issue 1 — CI/CD pipeline did nothing on push (module 2)

**Symptom.** Pushing to `feature/bad-code` produced no review. No error anywhere: the
trigger was registered and looked correct, and an open PR existed.

### False lead: "Docker isn't installed"

```
docker   NOT INSTALLED
daemon UNAVAILABLE
```

That looks fatal, since every module has a Dockerfile. **It is irrelevant.** The
`build-image.sh` scripts zip the Dockerfile to S3 and AWS builds the image
server-side. All four modules build with no local Docker. Confirmed by
`create-microvm-image` returning `SUCCESSFUL`.

### False lead: "python3 is 3.9, the template needs 3.13"

```
python3     Python 3.9.25      <- default
python3.13  Python 3.13.15     <- also present
```

SAM resolves `BuildMethod: python3.13` against `python3.13` on `PATH`, not the default
`python3`. `sam build` succeeded unchanged.

### Root cause

Working the chain backwards, the trigger and PR were fine, so the target was checked:

```bash
$ aws lambda get-policy --function-name durable-orchestrator --qualifier live
An error occurred (ResourceNotFoundException) ... The resource you requested does not exist.
```

The **function did not exist**. Neither did its CloudFormation stack — only the four
workshop bootstrap stacks were present. The trigger pointed at
`...:function:durable-orchestrator:live`, an ARN that resolved to nothing, so pushes
had nowhere to go and CodeCommit reported nothing back.

Step 2 of the chain (`orchestrator/deploy.sh`) had never completed, while step 3
(`wire-codecommit.sh`) had run at some earlier point.

### Fix

```bash
cd module-2/orchestrator && sam build && sam deploy \
  --parameter-overrides "ImageArn=$IMAGE_ARN" "MvmExecutionRoleArn=$MVM_EXECUTION_ROLE_ARN"
# => stack durable-orchestrator CREATE_COMPLETE

cd ../trigger && ./wire-codecommit.sh
# =>     permission added
# => {"successfulExecutions":["ai-review-trigger"],"failedExecutions":[]}
```

Both steps were required. Deploying recreates the function with **no resource policy**,
so the wiring must be re-run even though the trigger still looks right in CodeCommit.

**Verified:** a push produced a full Claude security review on PR #1 (SQL injection,
command injection, hardcoded credentials, `eval`, unsalted MD5), and the reviewer VM
terminated cleanly.

### Takeaway

`aws lambda get-policy --function-name <fn> --qualifier <alias>` is the fastest probe
for this whole class of bug. A registered trigger proves nothing about the target.

---

## Issue 2 — MicroVM died 2 seconds after launch (module 1)

Found while enabling lifecycle hooks on module 1's image.

**Symptom.** The VM reached `PENDING`, then `TERMINATED` almost immediately.

```bash
$ aws lambda-microvms get-microvm --microvm-identifier microvm-3ba8368e...
"state": "TERMINATED",
"stateReason": "Run lifecycle hook returned HTTP status 404. Please check your hook
                endpoint and application logs for more details.",
"startedAt":    "2026-09-13T10:47:52.781000+00:00",
"terminatedAt": "2026-09-13T10:47:54.703000+00:00"
```

The VM log gave the rest:

```
"POST /aws/lambda-microvms/runtime/v1/ready HTTP/1.1" 200
"POST /aws/lambda-microvms/runtime/v1/run HTTP/1.1" 404
```

### Root cause

`app.py` handled `/launch`, but the runtime posts **`/run`**. The unmatched path fell
through to `else: send_json(404, ...)`, and **any non-200 from a lifecycle hook makes
Lambda destroy the VM.**

This stayed hidden because module 1's original image was built with **no hooks
configured** — nothing ever called the hook server. Enabling hooks exposed it.

### Fix

```python
elif self.path.endswith("/run") or self.path.endswith("/launch"):
    MICROVM_ID = body.get("microVmId")
    self.send_json(200, {"status": "launched"})
...
else:
    # Never 404 here: a non-200 from any hook terminates the MicroVM.
    logger.warning(f"unknown hook {self.path} — returning 200")
    self.send_json(200, {"status": "ok", "hook": self.path})
```

**Verified in service.** Image republished as version 3.0; the VM reached `RUNNING`
and stayed there, with `"POST /aws/lambda-microvms/runtime/v1/run HTTP/1.1" 200`.

---

## Issue 3 — `/health` sent two HTTP responses (module 1)

Spotted while reading the same handler.

```python
if self.path.endswith("/health"):
    self.send_json(200, {"status": "I'm here!"})   # response #1

if self.path.endswith("/ready"):                  # new chain, not elif
    ...
else:
    self.send_json(404, {"error": "unknown hook"})  # response #2 — same socket
```

A `/health` request matched the first `if`, then fell through the second chain to
`else`, writing **200 followed by 404** on one connection. Whether the caller sees
success or failure depends on how it parses the stream.

**Fix:** `if` → `elif`, joining the chains.

**Verified** by driving the handler over a raw socket and counting status lines:

```
OK  ../ready            responses=1  HTTP/1.0 200 OK
OK  ../run              responses=1  HTTP/1.0 200 OK
OK  ../suspend          responses=1  HTTP/1.0 200 OK
OK  ../resume           responses=1  HTTP/1.0 200 OK
OK  ../terminate        responses=1  HTTP/1.0 200 OK
OK  ../health           responses=1  HTTP/1.0 200 OK
OK  ../some-future-hook responses=1  HTTP/1.0 200 OK
```

---

## Issue 4 — the `run` hook has no `microVmId`

The workshop code assumes it does:

```python
MICROVM_ID = body.get("microVmId")     # always None in practice
```

Logging the raw body showed no such field, and the VM environment carries only:

```
AWS_LAMBDA_MICROVM_IMAGE_NAME, AWS_LAMBDA_MICROVM_IMAGE_ARN,
AWS_LAMBDA_MICROVM_IMAGE_VERSION, AWS_REGION
```

No VM id anywhere. Two consequences:

1. `json.loads()` on an **empty** body raises, which would fail the hook and kill the
   VM. `read_body` now tolerates empty and malformed payloads.
2. If the app needs its own id, the caller must supply it. Module 4 does this properly —
   the control plane stamps `X-Microvm-Id` on each proxied request.

---

## Non-issues confirmed by inspection

**Module 3's runner is correct.** It matches its job endpoint with `self.path == "/run"`
(exact), so the hook path `/aws/lambda-microvms/runtime/v1/run` falls through to the
catch-all `200`. Safe by construction — the better pattern.

**`MVM_EXECUTION_ROLE_ARN` pointing at a role named `...BuildRole...` is correct.**
`Module2ReviewerBuildRole-workshop` is the role holding `bedrock:InvokeModel`,
`lambda:SendDurableExecutionCallback*`, CodeCommit and STS permissions.
`LambdaMicroVMExecutionRole-workshop` has none of them. Misleading name, right value.

---

## Security findings

**Module 4's control plane is unauthenticated.** The HTTP API's `$default` route has no
authorizer, and tenant identity comes from the client-supplied `X-Tenant` header:

```bash
curl -H "X-Tenant: globex" "$CONTROL_PLANE_URL/api/files"   # any caller, any tenant
```

Data isolation *behind* the control plane is genuinely enforced by IAM session
policies — a VM scoped to `acme` cannot read `globex/`:

```
AccessDenied: ... /TenantAccessRole-workshop/tenant-acme is not authorized to perform:
s3:GetObject on ".../globex/notes.txt" because no session policy allows the action
```

But that only binds the tenant the header claims. For anything beyond a lab, add a
JWT/Cognito or Lambda authorizer and derive the tenant from **verified token claims**,
never from a raw header.

**Module 1 executes arbitrary code with no authentication** beyond the proxy token.
That is the lab's purpose and the MicroVM is the boundary, but do not expose it to
untrusted callers, and consider dropping `INTERNET_EGRESS` for real sandboxing.

---

## Diagnostic quick reference

```bash
# Is the pipeline target real, and can CodeCommit invoke it?
aws lambda get-function --function-name <fn>
aws lambda get-policy   --function-name <fn> --qualifier live

# Why did a VM disappear?  stateReason names the exact hook and status.
aws lambda-microvms get-microvm --microvm-identifier <id> --query '[state,stateReason]'

# What did the app inside the VM log?  (contains binary TLS noise)
aws logs tail /aws/lambda-microvms/<image-name> --since 10m | strings | grep -a POST

# Which image version is actually active?
aws lambda-microvms get-microvm-image --image-identifier <arn> \
  --query '[latestActiveImageVersion,state]'
```

Two habits that shortened everything: **read `stateReason` first** when a VM dies —
it names the hook and the status code — and **test HTTP handlers over a local socket**
before paying two minutes for an image rebuild.
