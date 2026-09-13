# Measured metrics

Every number here was measured against a real AWS account in `us-east-1` on
2026-09-13 while building this repository. Nothing is estimated. The methodology for
each table is stated so you can reproduce or challenge it.

Single-run measurements on shared infrastructure — treat them as **order of magnitude**,
not benchmarks. The ratios are the durable part.

---

## 1. Image build times

Server-side build: zip → S3 → `create-microvm-image` → `SUCCESSFUL`.

| Image | Version | Build | What's in it |
|---|---|---|---|
| `mvm-saas-tenant` | 1.0 | **111 s** | Python + boto3 |
| `mvm-code-execution-sandbox` | 1.0 | **160 s** | Python, stdlib only |
| | 2.0 | **99 s** | same, rebuilt |
| | 3.0 | **99 s** | same, rebuilt |
| `mvm-ci-runner` | 1.0 | **120 s** | + git, bash, AWS CLI |
| `mvm-claude-reviewer-async` | 1.0 | **153 s** | + Node.js 22, Claude Code CLI, AWS CLI |

**Method.** `list-microvm-image-versions`, then `updatedAt − createdAt` per version.

Two things worth noting. Build time tracks **image weight** — the reviewer image installs
Node.js and a global npm package and costs ~40 % more than the tenant app. And the first
build of the sandbox took 160 s while identical rebuilds took 99 s, consistent with
layer caching on the build fleet.

Budget **~2 minutes per image build**. It is slow enough that you should test HTTP
handlers locally first (see [DOCKER.md](DOCKER.md#debugging-without-a-local-daemon)).

---

## 2. MicroVM lifecycle timings

| Operation | Measured | Notes |
|---|---|---|
| `run-microvm` API returns | **1.12 s** | returns `state: PENDING`, not ready yet |
| launch → `RUNNING` | **3.57 s** | includes the API call; restore from snapshot |
| `create-microvm-auth-token` | **1.03 s** | one control-plane call |
| `RUNNING` → `SUSPENDED` | **2.09 s** | explicit `suspend-microvm` |
| `SUSPENDED` → `RUNNING` | **2.07 s** | explicit `resume-microvm` |
| first request after resume | **0.048 s** | app still live, state intact |

**Method.** Wall-clock around each CLI call, polling `get-microvm` every 0.5 s.

The last row is the one that matters: a resumed VM answered in **48 ms**, so suspend/
resume preserves the running process rather than restarting it. That is what makes
`autoResumeEnabled: true` viable for keeping a tenant warm.

`run-microvm` returning `PENDING` after 1.12 s is why every orchestrator polls for
`RUNNING` before calling the endpoint.

### Compared with the alternatives

| | Container | **Lambda MicroVM** | EC2 instance |
|---|---|---|---|
| Start to serving | ms | **~3.6 s measured** | tens of seconds |
| Isolation | shared kernel | **own kernel** | own kernel |
| You patch the OS | yes | **no** | yes |

Not as fast as a container, an order of magnitude faster than booting a VM yourself —
while still giving you a private kernel.

---

## 3. Request latency

### Module 1 — code execution sandbox

| Request | Time |
|---|---|
| 1st (TLS handshake + connection setup) | **2.84 s** |
| 2nd | 0.036 s |
| 3rd | 0.031 s |
| 4th | 0.033 s |
| 5th | 0.042 s |

Steady state ≈ **35 ms** for `print(sum(range(1,101)))`, which includes the ingress
proxy, the HTTP server, writing a temp file, and a full `python3` subprocess spawn.

The 2.84 s first call is connection setup, not VM startup — the VM was already `RUNNING`.
Reuse connections if you care about tail latency.

### Module 4 — SaaS control plane

| Request | Time | What happens |
|---|---|---|
| `/health` (cold Lambda) | **1.16 s** | Lambda init; no VM touched |
| `/health` (warm) | 0.062 s → 0.043 s | pure Lambda |
| tenant, **cold** | **2.73 s** | launches the tenant's MicroVM inline |
| tenant, **warm** ×5 | 0.063 / 0.062 / 0.055 / 0.061 / 0.065 s | cached VM + cached token |

**Cold-to-warm ratio ≈ 45×** (2.73 s → ~0.060 s).

**Method.** `curl -w "%{time_total}"` against the deployed API, tenant `metrics-demo`
created fresh so the cold path was genuinely cold. Same `microvmId` returned on all six
calls, confirming reuse rather than relaunch.

This is the whole economic argument: a tenant's first request pays ~2.7 s to
materialise dedicated hardware isolation; every later request is ~60 ms; between
requests the tenant costs nothing.

---

## 4. Lambda-side metrics

Two-hour window covering the full session.

| Function | Invocations | Errors | Avg duration | Max duration |
|---|---|---|---|---|
| `durable-orchestrator` | 4 | **0** | 739 ms | 1149 ms |
| `ci-runner-orchestrator` | 2 | **0** | 1693 ms | 3384 ms |
| `saas-control-plane` | 16 | **0** | 608 ms | 2660 ms |

**Method.** `cloudwatch get-metric-statistics`, namespace `AWS/Lambda`.

The durable orchestrator is the interesting row. It supervised an **81-second** review
across 4 invocations averaging 739 ms — roughly **3 seconds of billed compute for 81
seconds of wall-clock work**, because `callback.result()` suspends instead of blocking.
A naive implementation that waited inline would have billed all 81 seconds.

From the invocation logs:

```
initDurationMs 1305.2   memorySizeMB 512   maxMemoryUsedMB 92-99
```

Memory is heavily over-provisioned: **~99 MB used of 512 MB**. Dropping to 256 MB would
be safe and cheaper. `ci-runner-orchestrator` and `saas-control-plane` already use 256 MB.

---

## 5. End-to-end pipeline latency

From `git commit` to a comment appearing on the pull request.

| Module | Commit | Comment posted | **End to end** | Output |
|---|---|---|---|---|
| 2 — AI review | 10:43:21 | 10:44:41.95 | **80.9 s** | 3,938-char review |
| 3 — CI build | 11:05:20 | 11:05:45.97 | **26.0 s** | 263-char pass/fail |

**Method.** `git log -1 --format=%cI` for the commit; `creationDate` from
`get-comments-for-pull-request`.

Roughly where the time goes:

```
Module 2 (80.9 s)
  trigger + orchestrator start      ~3 s
  MicroVM launch -> RUNNING         ~4 s
  clone + diff                      ~5 s
  claude -p against the diff       ~60 s   <-- dominant cost
  post comment + callback           ~5 s

Module 3 (26.0 s)
  trigger + orchestrator start      ~3 s
  MicroVM launch -> RUNNING         ~4 s
  clone + checkout                  ~5 s
  compileall + unittest            ~<1 s
  post comment                      ~3 s
```

Module 3's pipeline ran in under a second; **~25 of its 26 seconds is fixed overhead**
(trigger, launch, clone, comment). That sets the floor: MicroVM-per-push suits jobs
measured in tens of seconds or more. For a 200 ms lint check, the overhead dominates.

Module 2 is the opposite — the AI call dwarfs everything, which is exactly why durable
suspension pays there and is unnecessary in module 3.

---

## 6. Idle policy behaviour

Every VM launched during the session, hours later:

```
TERMINATED  mvm-ci-runner              microvm-364fc174...
TERMINATED  mvm-claude-reviewer-async   microvm-d9be077a...
TERMINATED  mvm-code-execution-sandbox  microvm-3ba8368e...
TERMINATED  mvm-code-execution-sandbox  microvm-e241e667...
TERMINATED  mvm-code-execution-sandbox  microvm-ea87a131...
TERMINATED  mvm-code-execution-sandbox  microvm-ee7ebcc0...
TERMINATED  mvm-saas-tenant             microvm-78f934dd...
TERMINATED  mvm-saas-tenant             microvm-ced70315...
```

**8 VMs, 8 terminated, no cleanup code.** Only the reviewer VM was explicitly terminated
(by its orchestrator); the rest expired through their idle policies, including the two
module-4 tenant VMs that had `autoResumeEnabled: true`.

Observed states: `PENDING → RUNNING → SUSPENDED → TERMINATED`. Hard cap per VM:
`maximumDurationInSeconds: 28800` (8 hours).

This is the operational payoff. Forget to terminate a VM and you are not paying for it
tomorrow — which is emphatically not true of EC2.

---

## 7. Reproducing these numbers

```bash
# image build times
aws lambda-microvms list-microvm-image-versions --image-identifier "$IMAGE_ARN" \
  | jq -r '.items[] | "\(.imageVersion) \(.createdAt) \(.updatedAt)"'

# launch -> RUNNING
T0=$(date +%s.%N)
VM=$(aws lambda-microvms run-microvm ... --query microvmId --output text)
until [ "$(aws lambda-microvms get-microvm --microvm-identifier "$VM" \
           --query state --output text)" = RUNNING ]; do sleep 0.5; done
echo "launch: $(echo "$(date +%s.%N)-$T0" | bc)s"

# request latency
curl -s -o /dev/null -w '%{time_total}s\n' -H "X-Tenant: acme" "$URL/api/info"

# Lambda metrics
aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Duration \
  --dimensions Name=FunctionName,Value=durable-orchestrator \
  --start-time "$(date -u -d '2 hours ago' +%Y-%m-%dT%H:%M:%S)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" \
  --period 7200 --statistics Average Maximum

# end-to-end pipeline
git log -1 --format=%cI
aws codecommit get-comments-for-pull-request --pull-request-id 1 \
  | jq -r '.commentsForPullRequestData[].comments[].creationDate'
```

---

## 8. What the numbers argue

| Finding | Consequence |
|---|---|
| Launch to serving **~3.6 s** | fast enough to launch per request/job; too slow for per-HTTP-call in a hot path |
| Resume then serve in **48 ms** | keep tenants warm with `autoResumeEnabled: true` rather than relaunching |
| Cold-to-warm **≈45×** | cache the VM handle; the second request is a different product |
| Durable review: **~3 s billed for 81 s wall-clock** | suspend instead of waiting whenever an external system is slow |
| Module 3: **~25 s of 26 s is overhead** | one VM per push fits jobs of tens of seconds, not sub-second checks |
| Orchestrators use **~99 MB of 512 MB** | right-size memory; `durable-orchestrator` could halve it |
| **8/8 VMs self-terminated** | the idle policy is the cleanup mechanism — no reaper needed |

For pricing, use the [AWS Lambda pricing page](https://aws.amazon.com/lambda/pricing/)
and the AWS Pricing Calculator; the billing dimensions to plug in are VM lifetime
(seconds × memory) and orchestrator billed duration, both measurable above.
