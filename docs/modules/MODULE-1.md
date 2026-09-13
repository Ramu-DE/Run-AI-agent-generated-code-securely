# Module 1 — Sandboxed code execution

**Goal.** Run untrusted, AI-generated Python inside a hardware-isolated MicroVM and
get the result back over HTTPS.

**Why a MicroVM.** Executing code you did not write is the textbook case for strong
isolation. A container shares the host kernel, so a kernel-level escape reaches
everything else. A MicroVM has its **own kernel**, so the blast radius of a successful
escape is one throwaway VM.

Nothing is orchestrated here — you drive every step by hand. That is deliberate: the
later modules automate exactly these calls.

---

## 1. Architecture

```
        you (CLI / curl)
              |
              |  HTTPS + signed auth token
              v
   +---------------------------------------------+
   |  MicroVM (own kernel, aarch64, 4 vCPU)      |
   |                                             |
   |   port 8080  AppHandler                     |
   |     GET  /          -> status               |
   |     POST /execute   -> run code             |
   |                                             |
   |   port 9000  HookHandler                    |
   |     POST /aws/lambda-microvms/runtime/v1/*  |
   |          <- Lambda calls these, not you     |
   |                                             |
   |   execute_code() -> subprocess python3      |
   |                     30s timeout, temp file  |
   +---------------------------------------------+
```

Two servers in one image, on two ports, with two very different audiences: **8080 is
yours**, **9000 belongs to the Lambda runtime**. Running two listeners in one process
is normal for a VM and awkward for a container — a good early illustration of the
difference.

---

## 2. The files

| File | Role |
|---|---|
| `app.py` | both HTTP servers and the sandbox logic |
| `Dockerfile` | how the image filesystem is assembled |
| `requirements.txt` | Python dependencies (empty here — stdlib only) |

### `app.py`, the parts that matter

The execution core writes the code to a temp file and runs it as a **separate
process**, so a crash or `sys.exit()` cannot take down the HTTP server:

```python
def execute_code(code: str) -> dict:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        script_path = f.name
    try:
        result = subprocess.run(
            ["python3", script_path],
            capture_output=True,   # collect stdout/stderr instead of inheriting
            text=True,             # decode bytes to str
            timeout=30,            # a runaway loop cannot pin the VM forever
            cwd=tempfile.gettempdir(),
        )
        return {"stdout": result.stdout, "stderr": result.stderr,
                "exit_code": result.returncode, "success": result.returncode == 0}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Execution timed out after 30 seconds",
                "exit_code": -1, "success": False}
    finally:
        os.unlink(script_path)      # always clean up, even on timeout
```

Note the honesty of the contract: a failing script is **not** an API error. It returns
HTTP 200 with `success: false` and the real `exit_code`. Only a malformed *request*
(missing `code`) is a 400.

The lifecycle handler answers the Lambda runtime. **Every branch must return 200** —
see [../MICROVM.md](../MICROVM.md) for why a single 404 destroys the VM:

```python
def do_POST(self):
    global MICROVM_ID
    body = self.read_body()
    if self.path.endswith("/health"):
        self.send_json(200, {"status": "I'm here!"})
    elif self.path.endswith("/ready"):        # build-time: snapshot taken after this
        self.send_json(200, {"status": "ready"})
    elif self.path.endswith("/run") or self.path.endswith("/launch"):
        MICROVM_ID = body.get("microVmId")
        self.send_json(200, {"status": "launched", "microvm_id": MICROVM_ID})
    ...
    else:
        logger.warning(f"unknown hook {self.path} — returning 200")
        self.send_json(200, {"status": "ok"})  # never 404 a hook
```

### `Dockerfile`

```dockerfile
FROM python:3.12-slim          # small Debian base with Python preinstalled
WORKDIR /app                   # default dir for later COPY and for CMD
COPY requirements.txt .        # dependencies copied BEFORE code...
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .                  # ...so editing app.py doesn't rerun pip
EXPOSE 8080                    # documentation only; opens nothing
CMD ["python3", "app.py"]      # the process Lambda starts in the VM
```

`--no-cache-dir` matters because pip's cache would otherwise be baked into the layer,
inflating the image with files nothing reads at runtime.

---

## 3. Complete flow

```
  BUILD (once)                          RUN (many times)
  ------------                          ----------------
  1. zip app.py + Dockerfile            5. run-microvm
  2. upload zip to S3                   6. poll get-microvm until RUNNING
  3. create-microvm-image               7. create-microvm-auth-token
     - AWS builds the Dockerfile        8. curl POST /execute
     - starts the app                   9. VM idles -> SUSPENDED -> TERMINATED
     - calls /ready on :9000
     - takes a memory SNAPSHOT
  4. poll until state SUCCESSFUL
```

The snapshot is the key idea. Step 3 boots your app once and freezes RAM. Every launch
in step 5 **restores that frozen memory** rather than cold-booting, which is why a VM
is serving traffic in ~1–2 seconds.

---

## 4. Step-by-step, with the purpose of every command

### Step 0 — confirm the environment

```bash
aws lambda-microvms help >/dev/null && echo "service available"
echo "$AWS_REGION / $AWS_ACCOUNTID / $ARTIFACTS_BUCKET"
```

`lambda-microvms` is a newer service; an older AWS CLI simply won't have it. Checking
first avoids a confusing "Invalid choice" error later.

You do **not** need Docker installed. See [../DOCKER.md](../DOCKER.md).

### Step 1 — package the build context

```bash
cd module-1
zip -qr app.zip app.py Dockerfile requirements.txt
```

| Part | Purpose |
|---|---|
| `zip` | this archive **is** the Docker build context AWS will build |
| `-q` | quiet; suppress the file-by-file listing |
| `-r` | recurse into directories |

Only what you list exists inside the build. Omit `requirements.txt` and the
`COPY requirements.txt .` line fails.

### Step 2 — upload the context to S3

```bash
aws s3 cp app.zip "s3://$ARTIFACTS_BUCKET/deployments/app.zip"
```

S3 is how you hand the build context to the service. The **build role** (next step)
must be able to `s3:GetObject` this key, or the build fails before it starts.

### Step 3 — create the image

```bash
aws lambda-microvms create-microvm-image \
  --name mvm-code-execution-sandbox \
  --code-artifact "uri=s3://$ARTIFACTS_BUCKET/deployments/app.zip" \
  --base-image-arn "arn:aws:lambda:$AWS_REGION:aws:microvm-image:al2023-1" \
  --build-role-arn "arn:aws:iam::$AWS_ACCOUNTID:role/LambdaMicroVMBuildRole-workshop" \
  --hooks '{"port":9000,
            "microvmHooks":{"run":"ENABLED","runTimeoutInSeconds":5,
                            "terminate":"ENABLED","terminateTimeoutInSeconds":5},
            "microvmImageHooks":{"ready":"ENABLED","readyTimeoutInSeconds":60}}' \
  --egress-network-connectors "arn:aws:lambda:$AWS_REGION:aws:network-connector:aws-network-connector:INTERNET_EGRESS" \
  --resources '[{"minimumMemoryInMiB":2048}]' \
  --logging '{"cloudWatch":{"logGroup":"/aws/lambda-microvms/mvm-code-execution-sandbox"}}'
```

| Flag | Purpose |
|---|---|
| `--name` | logical image name; becomes part of the image ARN |
| `--code-artifact` | S3 location of the zip, in `uri=` form |
| `--base-image-arn` | AWS-managed base (`al2023-1`) providing kernel + init |
| `--build-role-arn` | role **AWS assumes to build**; needs `s3:GetObject` on the zip |
| `--hooks` | which lifecycle hooks fire, on which port, with what timeouts |
| `--egress-network-connectors` | grants outbound internet. **Omit to sandbox harder** |
| `--resources` | `minimumMemoryInMiB` — floor for VM memory |
| `--logging` | CloudWatch log group for build output *and* app stdout |

Inside `--hooks`:

- `microvmImageHooks.ready` — fires at **build** time. The snapshot is taken once your
  app answers 200, so this is your "I have finished starting" signal.
  `readyTimeoutInSeconds: 60` is how long AWS waits before failing the build.
- `microvmHooks.run` — fires at **launch** time, after restore from snapshot.
- `microvmHooks.terminate` — fires before shutdown, for flushing.

> Hooks are optional. Module 1's original image was built with none, which is exactly
> why the `/run`-vs-`/launch` bug stayed hidden until they were switched on.

The response returns immediately with an `imageArn`; the build runs asynchronously.

### Step 4 — wait for the build

```bash
aws lambda-microvms get-microvm-image-version \
  --image-identifier <imageArn> \
  --image-version 1.0 \
  --query state --output text
```

| Flag | Purpose |
|---|---|
| `--image-identifier` | which image |
| `--image-version` | which numbered version (`1.0` for a fresh `create`) |
| `--query state` | JMESPath to print just the state |
| `--output text` | bare string, ideal for `if`/`case` in shell |

States: `IN_PROGRESS → SUCCESSFUL` or `FAILED`. Typically 90–120 s. On `FAILED`, the
Docker build output is in the CloudWatch group from `--logging`:

```bash
aws logs tail /aws/lambda-microvms/mvm-code-execution-sandbox --since 10m | strings
```

`strings` is needed because the log stream also carries binary TLS handshake noise.

### Step 5 — launch a VM

```bash
RUN=$(aws lambda-microvms run-microvm \
  --image-identifier "arn:aws:lambda:$AWS_REGION:$AWS_ACCOUNTID:microvm-image:mvm-code-execution-sandbox" \
  --image-version 3.0 \
  --ingress-network-connectors "arn:aws:lambda:$AWS_REGION:aws:network-connector:aws-network-connector:HTTP_INGRESS" \
  --egress-network-connectors "arn:aws:lambda:$AWS_REGION:aws:network-connector:aws-network-connector:INTERNET_EGRESS" \
  --execution-role-arn "arn:aws:iam::$AWS_ACCOUNTID:role/LambdaMicroVMExecutionRole-workshop" \
  --idle-policy '{"maxIdleDurationSeconds":600,"suspendedDurationSeconds":300,"autoResumeEnabled":true}')

VM=$(echo "$RUN" | jq -r .microvmId)
EP=$(echo "$RUN" | jq -r .endpoint)
```

| Flag | Purpose |
|---|---|
| `--image-version` | **which version to boot.** Launching `1.0` after publishing `3.0` silently runs old code |
| `--ingress-network-connectors` | `HTTP_INGRESS` gives the VM a public HTTPS endpoint. Without it nothing can reach in |
| `--execution-role-arn` | the role the **running VM** assumes; its credentials appear inside the VM automatically |
| `--idle-policy` | lifetime automation, below |

Idle policy fields:

| Field | Meaning |
|---|---|
| `maxIdleDurationSeconds` | no traffic for this long → **SUSPENDED** |
| `suspendedDurationSeconds` | suspended this long → **TERMINATED** |
| `autoResumeEnabled` | `true` = next request resumes it; `false` = one-shot VM |

This is why you never pay for an idle sandbox and never write cleanup code.

### Step 6 — wait for RUNNING

```bash
until [ "$(aws lambda-microvms get-microvm --microvm-identifier "$VM" \
           --query state --output text)" = RUNNING ]; do sleep 3; done
```

`run-microvm` returns while state is still `PENDING`. Calling the endpoint too early
fails. If the VM dies instead, `get-microvm` tells you exactly why:

```bash
aws lambda-microvms get-microvm --microvm-identifier "$VM" --query '[state,stateReason]'
```

`stateReason` is the single most useful field in this whole API — it names the failing
hook and its HTTP status.

### Step 7 — mint an auth token

```bash
TOKEN=$(aws lambda-microvms create-microvm-auth-token \
  --microvm-identifier "$VM" \
  --expiration-in-minutes 60 \
  --allowed-ports '[{"port":8080}]' \
  --query 'authToken."X-aws-proxy-auth"' --output text)
```

| Flag | Purpose |
|---|---|
| `--expiration-in-minutes` | short TTL; a leaked token expires on its own |
| `--allowed-ports` | **ports this token may reach.** A token for 8080 cannot touch 9000 |
| `--query 'authToken."X-aws-proxy-auth"'` | pull out the header value (quoted because the key contains `-`) |

The endpoint is public, so the token *is* the authentication. Scoping it to 8080 keeps
callers away from the lifecycle port.

### Step 8 — call the sandbox

```bash
curl -s -X POST "https://$EP/execute" \
  -H "X-aws-proxy-auth: $TOKEN" \
  -H "X-aws-proxy-port: 8080" \
  -H 'Content-Type: application/json' \
  -d '{"code":"print(sum(range(1,101)))"}'
```

| Header | Purpose |
|---|---|
| `X-aws-proxy-auth` | the signed token from step 7 |
| `X-aws-proxy-port` | **which port inside the VM** to route to; must be in `--allowed-ports` |

Forgetting `X-aws-proxy-port` is a common mistake — the proxy needs to be told the
destination port explicitly.

---

## 5. Verified behaviour

```bash
# success
{"stdout": "5050\n", "stderr": "", "exit_code": 0, "success": true}

# failing code: still HTTP 200, but success=false with the real traceback
{"stdout": "", "stderr": "...ValueError: intentional failure\n", "exit_code": 1, "success": false}

# bad request: HTTP 400
{"error": "code field is required"}
```

Proof it is a real VM, obtained by running introspection code *through* the sandbox:

```json
{"stdout": "machine: aarch64\nkernel: 6.1.166-24.303.amzn2023.aarch64\nhostname: localhost\ncpus: 4\n0::/system.slice/app\n"}
```

Its own kernel and 4 vCPUs — not a container sharing yours.

---

## 6. Publishing a new version

`create-microvm-image` is for the **first** build. To ship a change, use
`update-microvm-image`, which publishes a **new version** rather than mutating:

```bash
aws lambda-microvms update-microvm-image \
  --image-identifier "$IMAGE_ARN" \
  --code-artifact "uri=s3://$ARTIFACTS_BUCKET/deployments/app-new.zip" \
  --base-image-arn "arn:aws:lambda:$AWS_REGION:aws:microvm-image:al2023-1" \
  --build-role-arn "arn:aws:iam::$AWS_ACCOUNTID:role/LambdaMicroVMBuildRole-workshop" \
  --description "what changed" \
  --hooks '...' --resources '...' --logging '...'
```

It returns `"state": "UPDATING"` and a new `imageVersion` (`2.0`, then `3.0`, …).
Existing versions keep working, so rollback is just launching the previous number.

Check which version is current:

```bash
aws lambda-microvms get-microvm-image --image-identifier "$IMAGE_ARN" \
  --query '[latestActiveImageVersion,state]'
```

---

## 7. Failure modes seen in practice

| Symptom | Cause | Fix |
|---|---|---|
| `TERMINATED` ~2 s after launch, `stateReason` names a hook and 404 | a lifecycle hook returned non-200 | handle the real path (`/run`, not `/launch`); default unknown hooks to 200 |
| Image build `FAILED` | Dockerfile error, or build role can't read the zip | read the build log in CloudWatch; check `s3:GetObject` |
| Calls to the endpoint hang or 403 | VM not `RUNNING` yet, or token/port mismatch | poll for `RUNNING`; ensure `X-aws-proxy-port` ∈ `--allowed-ports` |
| Code runs but has no internet | `INTERNET_EGRESS` not attached | add the egress connector — or leave it off deliberately |
| New code has no effect | launched an older `--image-version` | check `latestActiveImageVersion` |

Both module-1 hook bugs and their evidence are in
[../TROUBLESHOOTING.md](../TROUBLESHOOTING.md).

---

## 8. Security notes

This service **executes arbitrary caller-supplied code by design**. The MicroVM is the
boundary, and that part is sound. Before reusing the pattern:

- **Add authentication.** There is none beyond the proxy token; anyone holding it gets
  arbitrary code execution.
- **Drop `INTERNET_EGRESS`** unless the workload truly needs it. Without it, submitted
  code cannot exfiltrate data or fetch a second-stage payload.
- **Keep the execution role minimal.** Whatever it can reach, the submitted code can
  reach — credentials are injected into the VM automatically.
- **Keep the 30-second timeout**, and prefer a short `maxIdleDurationSeconds` so a VM
  that ran hostile code is destroyed quickly rather than reused.

---

## 9. Cleanup

```bash
aws lambda-microvms terminate-microvm --microvm-identifier "$VM"   # stop one now
aws lambda-microvms list-microvms                                  # see what's alive
aws lambda-microvms delete-microvm-image-version \
  --image-identifier "$IMAGE_ARN" --image-version 1.0              # drop an old version
```

Termination is optional — the idle policy gets there on its own.

**Next:** [MODULE-2.md](MODULE-2.md) automates all of the above from a Lambda and adds
durable orchestration.
