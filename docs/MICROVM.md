# Lambda MicroVMs — how they actually work

Everything here was confirmed against the running service while building this
repository, not paraphrased from docs.

## What a MicroVM is

A **Firecracker virtual machine** that AWS Lambda launches from an image you build
from a Dockerfile. You get a real kernel and a real init process, so long-running
and multi-process workloads are fine.

From inside a module-1 VM:

```
machine:  aarch64          <- MicroVMs run on ARM64
kernel:   6.1.166-24.303.amzn2023.aarch64
hostname: localhost
cpus:     4
cgroup:   0::/system.slice/app
```

### Where it sits between containers and VMs

| | Container | **Lambda MicroVM** | EC2 instance |
|---|---|---|---|
| Isolation | shared kernel | **own kernel (hardware)** | own kernel |
| Start time | ms | **~1–2 s from snapshot** | tens of seconds |
| Lifetime | you manage | **idle policy, up to 8 h** | you manage |
| You patch the OS | yes | **no** | yes |
| Billing | while running | **while the VM exists** | while running |

Max lifetime observed on a launched VM: `maximumDurationInSeconds: 28800` (8 hours).

## Images and versions

An image has a **name/ARN** and a stack of numbered **versions**.

```bash
# First time — creates the image at version 1.0
aws lambda-microvms create-microvm-image --name mvm-code-execution-sandbox ...

# Later — publishes 2.0, 3.0, ... It does NOT mutate the existing version.
aws lambda-microvms update-microvm-image --image-identifier <arn> ...
```

This bit is easy to trip over: `update-microvm-image` returns
`"state": "UPDATING"` with a **new** `imageVersion`, and `run-microvm` takes an
explicit `--image-version`. Launching `1.0` after publishing `3.0` silently runs the
old code. Track the version you intend to run.

Build state moves `IN_PROGRESS → SUCCESSFUL | FAILED`, typically 90–120 s:

```bash
aws lambda-microvms get-microvm-image-version \
  --image-identifier <arn> --image-version 3.0 --query state --output text
```

The build happens **inside AWS**, assuming your `--build-role-arn`. Your only inputs
are a zip in S3 containing the Dockerfile and app, so no local Docker daemon is
needed — see [DOCKER.md](DOCKER.md).

## Lifecycle hooks — the part that bites

Enable hooks and Lambda POSTs to a port inside the VM at defined moments. **A hook
that answers with anything other than 200 kills the VM.** That is the single most
important rule here.

```json
{
  "port": 9000,
  "microvmImageHooks": { "ready": "ENABLED", "readyTimeoutInSeconds": 60 },
  "microvmHooks": {
    "run": "ENABLED", "runTimeoutInSeconds": 5,
    "terminate": "ENABLED", "terminateTimeoutInSeconds": 5
  }
}
```

### The real paths

Hooks arrive at `/aws/lambda-microvms/runtime/v1/<hook>`, observed in the VM log:

```
"POST /aws/lambda-microvms/runtime/v1/ready HTTP/1.1" 200
"POST /aws/lambda-microvms/runtime/v1/run HTTP/1.1" 200
```

| Hook | When | Purpose |
|---|---|---|
| `ready` | during the **image build** | app is up; the snapshot is taken after this returns 200 |
| `run` | after each launch from the snapshot | reset per-VM state, re-seed randomness |
| `suspend` | before suspension | flush state, close connections |
| `resume` | after resumption | reopen connections |
| `terminate` | before shutdown | flush logs and metrics |

`ready` is a **build-time** hook, so a failure there fails the image build. `run` is a
**runtime** hook, so a failure there destroys the VM you just launched.

### Two failure modes proven in practice

**1. Wrong path → instant death.** Module 1 originally handled `/launch`, but the
runtime posts `/run`. With hooks enabled the VM died 2 seconds after launch:

```
state:       TERMINATED
stateReason: Run lifecycle hook returned HTTP status 404.
log:         "POST /aws/lambda-microvms/runtime/v1/run HTTP/1.1" 404
```

**2. Returning 404 for unknown hooks is a trap.** If AWS adds a hook your handler
does not recognise, a 404 default kills the VM. Default to 200 instead:

```python
def do_POST(self):
    body = self.read_body()
    if self.path.endswith("/ready"):
        self.send_json(200, {"status": "ready"})
    elif self.path.endswith("/run") or self.path.endswith("/launch"):
        self.send_json(200, {"status": "launched"})
    elif self.path.endswith("/terminate"):
        self.send_json(200, {"status": "terminating"})
    else:
        # Never 404 here: a non-200 from any hook terminates the MicroVM.
        logger.warning(f"unknown hook {self.path} — returning 200")
        self.send_json(200, {"status": "ok"})
```

Module 3's runner shows the tidiest version of this idea: it matches its own job
endpoint with `self.path == "/run"` (exact) and returns 200 for *every* other POST,
so lifecycle hooks can never break it.

Also make hook bodies non-fatal. The `run` payload was **empty** in practice, so
`json.loads()` on it raises and the hook fails:

```python
def read_body(self) -> dict:
    raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}                     # log it, but never fail the hook
```

### Identifying the VM from inside

The `run` payload did **not** contain a `microVmId`. The VM environment exposes only:

```
AWS_LAMBDA_MICROVM_IMAGE_NAME=mvm-code-execution-sandbox
AWS_LAMBDA_MICROVM_IMAGE_ARN=arn:aws:lambda:...:microvm-image:mvm-code-execution-sandbox
AWS_LAMBDA_MICROVM_IMAGE_VERSION=3.0
AWS_REGION=us-east-1
```

If the app needs its own VM id, have the caller pass it. Module 4 does exactly that —
the control plane stamps `X-Microvm-Id` on every proxied request.

## Networking

Connectors are explicit; there is no ambient connectivity.

```
ingress: arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:HTTP_INGRESS
egress:  arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:INTERNET_EGRESS
```

Omit `INTERNET_EGRESS` and the VM cannot reach the internet — a sensible default when
running untrusted code. Each VM gets an HTTPS `endpoint` hostname.

## Authentication to a VM

Every call is signed with a short-lived token bound to specific ports:

```bash
TOKEN=$(aws lambda-microvms create-microvm-auth-token \
  --microvm-identifier "$VM" --expiration-in-minutes 60 \
  --allowed-ports '[{"port":8080}]' \
  --query 'authToken."X-aws-proxy-auth"' --output text)

curl -H "X-aws-proxy-auth: $TOKEN" -H "X-aws-proxy-port: 8080" "https://$ENDPOINT/"
```

`X-aws-proxy-port` selects the port **inside** the VM, and it must be one of
`--allowed-ports`. A token scoped to 9000 cannot reach 8080.

Note the proxy also speaks TLS to your port, so plain HTTP handlers log occasional
binary garbage and `400 Bad request version` lines. Harmless, but it means MicroVM
CloudWatch logs contain binary — pipe through `strings` before `grep`.

## Idle policy and states

```json
{"maxIdleDurationSeconds": 900, "suspendedDurationSeconds": 300, "autoResumeEnabled": true}
```

- idle for `maxIdleDurationSeconds` → **SUSPENDED**
- next request with `autoResumeEnabled: true` → **RUNNING** again
- suspended for `suspendedDurationSeconds` → **TERMINATED**

States seen: `PENDING → RUNNING → SUSPENDED → TERMINATED`, plus `stateReason` on
unexpected termination — always read it when a VM disappears.

Tune it to the workload:

| Workload | Policy |
|---|---|
| One-shot CI job (module 3) | short idle, `autoResumeEnabled: false` — run once and vanish |
| Warm tenant app (module 4) | long idle, `autoResumeEnabled: true` — survive gaps between requests |

## Command reference

```bash
aws lambda-microvms list-microvm-images
aws lambda-microvms get-microvm-image          --image-identifier <arn>
aws lambda-microvms get-microvm-image-version  --image-identifier <arn> --image-version 3.0
aws lambda-microvms create-microvm-image  ...      # new image at 1.0
aws lambda-microvms update-microvm-image  ...      # publishes 2.0, 3.0, ...
aws lambda-microvms run-microvm           ...      # launch
aws lambda-microvms get-microvm      --microvm-identifier <id>   # includes stateReason
aws lambda-microvms list-microvms
aws lambda-microvms suspend-microvm  --microvm-identifier <id>
aws lambda-microvms resume-microvm   --microvm-identifier <id>
aws lambda-microvms terminate-microvm --microvm-identifier <id>
aws lambda-microvms create-microvm-auth-token --microvm-identifier <id> --allowed-ports '[{"port":8080}]'
```

## IAM: two distinct roles

| Role | Used by | Needs |
|---|---|---|
| **build role** | the image build inside AWS | `s3:GetObject` on the artifacts bucket |
| **execution role** | the running VM | whatever your app calls (Bedrock, CodeCommit, STS…) |

The VM receives the execution role's credentials automatically, so the AWS SDK and
CLI work inside the VM with no keys baked into the image. Both roles trust
`lambda.amazonaws.com` for `sts:AssumeRole` and `sts:TagSession`.

Beware the workshop's naming: `Module2ReviewerBuildRole-workshop` is the role that
actually holds `bedrock:InvokeModel` and `lambda:SendDurableExecutionCallback*`, so it
is the correct **execution** role for modules 2–4 despite the name.
