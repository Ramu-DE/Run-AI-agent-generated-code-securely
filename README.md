# AWS Lambda MicroVMs Workshop — Working Build

Four progressively richer labs built on **AWS Lambda MicroVMs**: a hardware-isolated
Firecracker VM that Lambda launches from a container image you supply, keeps warm,
suspends, resumes, and terminates for you.

Every module in this repository has been **deployed and verified end to end** against
a real AWS account. Two genuine bugs were found and fixed along the way — both are
documented in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

| Module | What it builds | Core idea | Full walkthrough |
|---|---|---|---|
| [module-1](module-1) | Sandboxed code-execution API | MicroVM lifecycle + hooks | **[MODULE-1.md](docs/modules/MODULE-1.md)** |
| [module-2](module-2) | AI code reviewer on pull requests | Durable orchestration + callback pattern | **[MODULE-2.md](docs/modules/MODULE-2.md)** |
| [module-3-ci-runner](module-3-ci-runner) | Ephemeral CI runner | One disposable VM per push | **[MODULE-3.md](docs/modules/MODULE-3.md)** |
| [module-4-saas](module-4-saas) | Multi-tenant SaaS control plane | One VM per tenant + token vending machine | **[MODULE-4.md](docs/modules/MODULE-4.md)** |

Start with **[docs/modules/](docs/modules/README.md)** — a walkthrough per module giving
the architecture, the complete flow, and the purpose of every command and flag.

Concept guides:

- **[docs/MICROVM.md](docs/MICROVM.md)** — what a MicroVM is, images vs versions, the
  lifecycle hook contract, auth tokens, network connectors, idle policy.
- **[docs/DOCKER.md](docs/DOCKER.md)** — Docker basics, and why these Dockerfiles are
  built by AWS rather than by a local Docker daemon.
- **[docs/CICD.md](docs/CICD.md)** — the CI/CD pipelines, trigger wiring, and durable execution.
- **[docs/COMMANDS.md](docs/COMMANDS.md)** — every command used, with the purpose of each flag.
- **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — the failures hit and how they were diagnosed.

---

## The mental model

A MicroVM is **not** a container and **not** a Lambda function.

```
   Your Dockerfile + app.py
            |
            |  zipped -> S3 -> "aws lambda-microvms create-microvm-image"
            v
   MicroVM image (versioned: 1.0, 2.0, 3.0 ...)
            |
            |  "run-microvm"  -> boots from a snapshot in ~1-2s
            v
   A running Firecracker VM with its own kernel, own filesystem, own
   IAM execution role, reachable over HTTPS through a signed proxy token
```

The image is built from a container image, but what runs is a **full virtual machine**.
Verified from inside a module-1 VM:

```
machine: aarch64
kernel:  6.1.166-24.303.amzn2023.aarch64
cpus:    4
```

Long-running, stateful, and multi-process workloads all work — the things a Lambda
function cannot do — while you still pay only while the VM exists.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| AWS CLI with the `lambda-microvms` service model | verify: `aws lambda-microvms help` |
| AWS SAM CLI | used by every `deploy.sh` |
| `python3.13` on `PATH` | required by the SAM templates (`BuildMethod: python3.13`) |
| `zip`, `jq`, `git` | used by the `build-image.sh` scripts |
| A local Docker daemon | **not required** — see [docs/DOCKER.md](docs/DOCKER.md) |

Environment variables (the workshop bootstrap sets these in `~/.bashrc`):

```bash
export AWS_REGION=us-east-1
export AWS_ACCOUNTID=<your-account-id>
export ARTIFACTS_BUCKET=lambda-mvm-workshop-artifacts-$AWS_ACCOUNTID
export FUNCTION_NAME=durable-orchestrator
export MVM_EXECUTION_ROLE_ARN=arn:aws:iam::$AWS_ACCOUNTID:role/Module2ReviewerBuildRole-workshop
export MODULE2_REVIEWER_BUILD_ROLE_ARN=$MVM_EXECUTION_ROLE_ARN
```

> The role named `...BuildRole...` is genuinely the correct **execution** role for
> modules 2–4: it is the one carrying `bedrock:InvokeModel`,
> `lambda:SendDurableExecutionCallback*`, CodeCommit and STS permissions. The
> similarly named `LambdaMicroVMExecutionRole-workshop` has none of those and suits
> module 1 only. The naming is confusing; the mapping above is what works.

---

## Module 1 — Sandboxed code execution

An HTTP service inside a MicroVM that executes untrusted Python in a subprocess.
Port 8080 serves the app; port 9000 answers lifecycle hooks.

```bash
cd module-1
zip -qr app.zip app.py Dockerfile requirements.txt
aws s3 cp app.zip "s3://$ARTIFACTS_BUCKET/deployments/app.zip"

aws lambda-microvms create-microvm-image \
  --name mvm-code-execution-sandbox \
  --code-artifact "uri=s3://$ARTIFACTS_BUCKET/deployments/app.zip" \
  --base-image-arn "arn:aws:lambda:$AWS_REGION:aws:microvm-image:al2023-1" \
  --build-role-arn "arn:aws:iam::$AWS_ACCOUNTID:role/LambdaMicroVMBuildRole-workshop" \
  --hooks '{"port":9000,"microvmHooks":{"run":"ENABLED","runTimeoutInSeconds":5,"terminate":"ENABLED","terminateTimeoutInSeconds":5},"microvmImageHooks":{"ready":"ENABLED","readyTimeoutInSeconds":60}}' \
  --egress-network-connectors "arn:aws:lambda:$AWS_REGION:aws:network-connector:aws-network-connector:INTERNET_EGRESS" \
  --resources '[{"minimumMemoryInMiB":2048}]'
```

Launch it, mint a token scoped to port 8080, and call it:

```bash
RUN=$(aws lambda-microvms run-microvm \
  --image-identifier arn:aws:lambda:$AWS_REGION:$AWS_ACCOUNTID:microvm-image:mvm-code-execution-sandbox \
  --image-version 3.0 \
  --ingress-network-connectors "arn:aws:lambda:$AWS_REGION:aws:network-connector:aws-network-connector:HTTP_INGRESS" \
  --egress-network-connectors "arn:aws:lambda:$AWS_REGION:aws:network-connector:aws-network-connector:INTERNET_EGRESS" \
  --execution-role-arn arn:aws:iam::$AWS_ACCOUNTID:role/LambdaMicroVMExecutionRole-workshop \
  --idle-policy '{"maxIdleDurationSeconds":600,"suspendedDurationSeconds":300,"autoResumeEnabled":true}')

VM=$(echo "$RUN" | jq -r .microvmId)
EP=$(echo "$RUN" | jq -r .endpoint)

# wait for RUNNING before calling it
until [ "$(aws lambda-microvms get-microvm --microvm-identifier "$VM" --query state --output text)" = RUNNING ]; do sleep 3; done

TOKEN=$(aws lambda-microvms create-microvm-auth-token --microvm-identifier "$VM" \
  --expiration-in-minutes 60 --allowed-ports '[{"port":8080}]' \
  --query 'authToken."X-aws-proxy-auth"' --output text)

curl -s -X POST "https://$EP/execute" \
  -H "X-aws-proxy-auth: $TOKEN" -H "X-aws-proxy-port: 8080" \
  -H 'Content-Type: application/json' \
  -d '{"code":"print(sum(range(1,101)))"}'
```

Verified responses:

```json
{"stdout": "5050\n", "stderr": "", "exit_code": 0, "success": true}
{"stdout": "", "stderr": "...ValueError: intentional failure\n", "exit_code": 1, "success": false}
{"error": "code field is required"}
```

> **Security note.** This service executes arbitrary caller-supplied code by design —
> that is the lab's point, and the MicroVM is the security boundary. It has no
> authentication of its own beyond the proxy auth token. Do not expose it to
> untrusted callers without adding authentication and egress restrictions.

> Full step-by-step flow with every command explained: **[docs/modules/MODULE-1.md](docs/modules/MODULE-1.md)**

## Module 2 — AI code review with durable orchestration

A push to `feature/bad-code` fires a CodeCommit trigger → a **durable** Lambda
launches a reviewer MicroVM → the VM runs Claude Code headless against the PR diff
→ it calls back → the orchestrator terminates the VM and finishes.

```bash
cd module-2/reviewer-claude && ./build-image.sh      # publishes the image, persists IMAGE_ARN
cd ../orchestrator          && ./deploy.sh           # sam build + sam deploy, 'live' alias
cd ../trigger               && ./wire-codecommit.sh  # add-permission + register trigger
                               ./prepare-review.sh   # stage vulnerable code
cd /tmp/code-review && git add -A && git commit -m "..." && git push origin feature/bad-code
```

The reviewer posted this to PR #1 (excerpt of a much longer comment):

```
## 🤖 Claude AI Code Review (durable orchestration)
**[CRITICAL] SQL injection** — string-formatted query in get_user
**[CRITICAL] Command injection** — subprocess.call(cmd, shell=True)
**[CRITICAL] Hardcoded credentials** — DB_PASSWORD / API_TOKEN committed
**[CRITICAL] Arbitrary code execution** — eval(expression)
**[HIGH] Weak password hashing** — unsalted MD5
```

The interesting mechanic: `callback.result()` **suspends the orchestrator** with no
compute billed while Claude works, then the VM wakes it via
`SendDurableExecutionCallbackSuccess`. Verified in the logs as several short
invocations rather than one long one, on runtime `python:3.13.DurableFunction.v40`.

> Full step-by-step flow with every command explained: **[docs/modules/MODULE-2.md](docs/modules/MODULE-2.md)**

## Module 3 — Ephemeral CI runner

One disposable VM per push. No idle fleet, no runner to patch.

```bash
cd module-3-ci-runner/runner       && ./build-image.sh
cd ../orchestrator                 && ./deploy.sh
cd ../trigger                      && ./wire-codecommit.sh && ./prepare-pipeline.sh
cd /tmp/ci-demo && git add ci src tests && git commit -m "..." && git push origin feature/ci-pipeline
```

The orchestrator opened PR #2 automatically and the runner posted:

```
## CI build passed (exit code 0)
Ran on an ephemeral Lambda MicroVM runner, commit `5554a2a8e1`.
== compile ==
== test ==
Ran 1 test in 0.000s
OK
```

Both module 2 and module 3 triggers coexist on the same repository, scoped to
different branches — the wiring scripts merge into the trigger set rather than
overwriting it:

```json
[{"name":"ai-review-trigger","branches":["feature/bad-code"],"events":["updateReference"]},
 {"name":"ci-runner-trigger","branches":["feature/ci-pipeline"],"events":["createReference","updateReference"]}]
```

> Full step-by-step flow with every command explained: **[docs/modules/MODULE-3.md](docs/modules/MODULE-3.md)**

## Module 4 — Multi-tenant SaaS

An API Gateway HTTP API in front of a Lambda control plane that gives **each tenant
its own MicroVM**, caches it warm, and reverse-proxies requests to it.

```bash
cd module-4-saas/tenant-app    && ./build-image.sh
cd ../control-plane            && ./deploy.sh     # prints the ControlPlaneUrl
```

Distinct tenants land on distinct VMs, and repeat requests reuse the warm one:

```bash
curl -H "X-Tenant: acme"   $URL/api/info   # microvm-78f934dd...
curl -H "X-Tenant: globex" $URL/api/info   # microvm-ced70315...
```

Data isolation uses the **token vending machine** pattern: the app calls
`sts:AssumeRole` on one shared role, passing a per-request **session policy**
scoped to `s3://bucket/<tenant>/*`. Effective permissions are the intersection, so
one role and one policy template isolate every tenant with no per-tenant IAM.
A cross-tenant attempt is denied by IAM, not by application code:

```
AccessDenied: User: .../TenantAccessRole-workshop/tenant-acme is not authorized to
perform: s3:GetObject on ".../globex/notes.txt" because no session policy allows
the s3:GetObject action
```

> **Security note — read before reusing this.** The HTTP API's `$default` route has
> **no authorizer**, so the control plane is publicly invokable, and tenant identity
> is taken from the caller-supplied `X-Tenant` header. Any caller can therefore
> impersonate any tenant. That is acceptable for a lab; for anything real, put a
> JWT/Cognito or Lambda authorizer on the route and derive the tenant from verified
> token claims, never from a raw request header.

> Full step-by-step flow with every command explained: **[docs/modules/MODULE-4.md](docs/modules/MODULE-4.md)**

---

## Verified results

| Check | Result |
|---|---|
| Module 1 `/execute` | `sum(range(1,101))` → `5050`; errors → `exit_code 1`; empty → `400` |
| Module 1 VM identity | aarch64, kernel 6.1.166, 4 vCPU |
| Module 1 lifecycle hooks | `ready`/`run`/`suspend`/`resume`/`terminate`/unknown → single `200` each |
| Module 2 | Claude review comment posted to PR #1; reviewer VM terminated cleanly |
| Module 3 | CI comment "build passed (exit code 0)" on auto-opened PR #2 |
| Module 4 isolation | separate VM per tenant; warm reuse; cross-tenant S3 access denied by IAM |

## Cleaning up

MicroVMs self-terminate through their idle policy. To remove everything else:

```bash
aws cloudformation delete-stack --stack-name durable-orchestrator
aws cloudformation delete-stack --stack-name ci-runner-orchestrator
aws cloudformation delete-stack --stack-name saas-control-plane
aws lambda-microvms terminate-microvm --microvm-identifier <id>   # to stop one early
```
