# Module 2 — AI code review with durable orchestration

**Goal.** A developer pushes to a PR branch. Minutes later, an AI review appears as a
comment on the pull request. No servers, no polling, no idle billing.

**The hard problem.** An AI review takes minutes, and a Lambda function caps at 15 and
bills for every second it waits. Module 2 solves that with **durable execution**: the
orchestrator suspends at the point where it waits, is billed nothing while suspended,
and is woken by a callback.

---

## 1. Architecture

```
  git push (feature/bad-code)
        |
        v
  CodeCommit repo ---- trigger: updateReference ----> durable-orchestrator:live
                                                              |
   +----------------------------------------------------------+
   |  Durable Lambda (Python 3.13)                            |
   |   1 find open PR for the branch                          |
   |   2 step: run_microvm            -----------------+      |
   |   3 wait_for_condition: RUNNING                   |      |
   |   4 step: create_auth_token                       |      |
   |   5 create_callback -> callback_id                |      |
   |   6 step: POST /review (fire-and-forget) --+      |      |
   |   7 callback.result()  <== SUSPENDS HERE   |      |      |
   |          (zero compute billed)             |      |      |
   |   8 step: terminate_microvm                |      |      |
   +--------------------------------------------|------|------+
                        ^                       |      |
                        |                       v      v
                        |         +-------------------------------+
                        |         |  Reviewer MicroVM  (port 9000)|
                        |         |   202 accepted, thread starts |
                        |         |   git clone + git diff        |
                        |         |   claude -p  (Bedrock)        |
                        |         |   post-comment-for-pull-req   |
                        +---------|   SendDurableExecutionCallback|
                    wakes it up   +-------------------------------+
```

The orchestrator and the VM are **peers exchanging a callback id**, not a caller
blocking on a callee. That inversion is the whole design.

---

## 2. The files

| Path | Role |
|---|---|
| `reviewer-claude/app.py` | reviewer agent: accepts a job, runs Claude, calls back |
| `reviewer-claude/Dockerfile` | image with git, AWS CLI, Node.js, Claude Code CLI |
| `reviewer-claude/build-image.sh` | zip → S3 → `create-microvm-image` → wait |
| `orchestrator/lambda_function.py` | the durable orchestrator |
| `orchestrator/template.yaml` | SAM template (durable config, alias) |
| `orchestrator/deploy.sh` | `sam build` + `sam deploy` |
| `trigger/wire-codecommit.sh` | invoke permission + repository trigger |
| `trigger/prepare-review.sh` | stages deliberately vulnerable code to review |

`reviewer-kiro/` is an alternative reviewer using the Kiro CLI instead of Claude Code;
the orchestration around it is identical.

---

## 3. Complete flow

```
  SETUP (once)                          PER PUSH (automatic)
  ------------                          --------------------
  1 build-image.sh                      A push to feature/bad-code
      zip -> S3 -> create-microvm-image  |
      persists IMAGE_ARN                 v
  2 orchestrator/deploy.sh              B CodeCommit fires the trigger
      sam build (python3.13)            C orchestrator finds the open PR
      sam deploy -> :live alias         D launches reviewer MicroVM
  3 wire-codecommit.sh                  E waits for RUNNING, mints token
      add-permission                    F creates callback, POSTs /review
      put-repository-triggers           G VM returns 202 immediately
      test-repository-triggers          H orchestrator SUSPENDS
  4 prepare-review.sh                   I VM: clone, diff, claude -p
      writes vulnerable file            J VM posts PR comment
  5 git commit && git push  ----------> K VM sends callback success
                                        L orchestrator wakes, terminates VM
```

**Order is load-bearing.** Step 3 needs the alias from step 2 to exist. Skipping step 2
leaves a trigger pointing at nothing, and pushes do nothing silently — the exact
failure documented in [../TROUBLESHOOTING.md](../TROUBLESHOOTING.md).

---

## 4. Step 1 — build the reviewer image

```bash
cd module-2/reviewer-claude && ./build-image.sh
```

### What the script does, and why

```bash
zip -qr reviewer.zip app.py Dockerfile          # the Docker build context
aws s3 cp reviewer.zip "s3://${ARTIFACTS_BUCKET}/${S3_KEY}"
```

`S3_KEY` embeds a timestamp (`mvm-claude-reviewer-async-20260913-101816.zip`) so each
build is a distinct object — useful when you need to prove which artifact produced
which version.

```bash
aws lambda-microvms create-microvm-image \
  --name "mvm-claude-reviewer-async" \
  --code-artifact "uri=s3://${ARTIFACTS_BUCKET}/${S3_KEY}" \
  --base-image-arn "arn:aws:lambda:${AWS_REGION}:aws:microvm-image:al2023-1" \
  --build-role-arn "${MODULE2_REVIEWER_BUILD_ROLE_ARN}" \
  --environment-variables "CLAUDE_CODE_USE_BEDROCK=1" \
  --hooks '{"port":9000,"microvmHooks":{"run":"ENABLED",...},"microvmImageHooks":{"ready":"ENABLED","readyTimeoutInSeconds":60}}' \
  --egress-network-connectors "...INTERNET_EGRESS" \
  --resources '[{"minimumMemoryInMiB":2048}]' \
  --logging "{\"cloudWatch\":{\"logGroup\":\"/aws/lambda-microvms/${IMAGE_NAME}\"}}"
```

Beyond the flags covered in [MODULE-1.md](MODULE-1.md#step-3--create-the-image):

| Flag | Purpose here |
|---|---|
| `--environment-variables CLAUDE_CODE_USE_BEDROCK=1` | tells Claude Code to authenticate via **Bedrock + IAM role**, so no API key is ever baked into the image |
| `--egress-network-connectors` | **required** — the VM must reach CodeCommit and Bedrock |
| `--resources minimumMemoryInMiB: 2048` | Node.js + Claude Code needs real headroom |
| `--hooks port 9000` | the reviewer serves the app *and* hooks on one port |

Then it blocks until the build finishes and persists the result:

```bash
sudo tee /etc/profile.d/mvm-image-arn.sh >/dev/null <<EOF
export IMAGE_ARN="${IMAGE_ARN}"
EOF
```

`/etc/profile.d/` is sourced by every new login shell, so `deploy.sh` finds `IMAGE_ARN`
even in a different terminal. `deploy.sh` also re-sources this file directly, covering
the case where your shell was already open before the build ran.

### The reviewer's Dockerfile

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y git curl unzip && rm -rf /var/lib/apt/lists/*

# aarch64 because MicroVMs are ARM64 — the x86_64 build would fail at runtime
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o awscliv2.zip \
    && unzip -q awscliv2.zip && ./aws/install && rm -rf awscliv2.zip aws/

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code    # the CLI the agent shells out to
RUN pip install --no-cache-dir 'boto3>=1.43.0'  # needed for SendDurableExecutionCallback*

ENV CLAUDE_CODE_USE_BEDROCK=1
ENV ANTHROPIC_MODEL=us.anthropic.claude-sonnet-4-6
```

Two deliberate choices worth copying: `boto3>=1.43.0` because the callback APIs are new,
and **no `AWS_REGION`** — the app sets it per request from the payload, keeping one
image usable in any region.

---

## 5. Step 2 — deploy the orchestrator

```bash
cd module-2/orchestrator && ./deploy.sh
```

```bash
sam build                       # installs requirements.txt, stages code
sam deploy \
  --region "${AWS_REGION}" \
  --parameter-overrides \
    "ImageArn=${IMAGE_ARN}" \
    "MvmExecutionRoleArn=${MVM_EXECUTION_ROLE_ARN}"
```

| Command / flag | Purpose |
|---|---|
| `sam build` | pip-installs dependencies against a **python3.13** toolchain into `.aws-sam/build` |
| `--parameter-overrides` | injects the image ARN and VM execution role into the template |
| `samconfig.toml` | supplies `stack_name`, `capabilities`, `resolve_s3`, so you don't repeat them |

### The template, annotated

```yaml
DurableOrchestrator:
  Type: AWS::Serverless::Function
  Metadata:
    BuildMethod: python3.13      # durable functions REQUIRE 3.13 at build AND runtime
  Properties:
    Runtime: python3.13
    Timeout: 900
    DurableConfig:
      ExecutionTimeout: 3600     # the whole review may span an hour
      RetentionPeriodInDays: 7   # execution history kept for replay/debugging
    Environment:
      Variables:
        MVM_IMAGE_ARN: !Ref ImageArn
        MVM_EXECUTION_ROLE_ARN: !Ref MvmExecutionRoleArn
    AutoPublishAlias: live       # publish a version + move 'live' on every deploy
```

Two traps encoded here:

- **`DurableConfig` is immutable.** Changing it forces a function *replacement*, which
  drops in-flight durable executions.
- **`Timeout: 900` and `ExecutionTimeout: 3600` are different clocks.** The first limits
  one invocation; the second limits the whole durable execution across suspensions.

`AutoPublishAlias: live` is what lets the trigger bind to a stable name
(`...:function:durable-orchestrator:live`) instead of a version number that changes
each deploy.

### How the orchestrator suspends

```python
@durable_execution
def lambda_handler(event: dict, context: DurableContext) -> dict:
    record = event["Records"][0]

    if record.get("eventName") == "TriggerEventTest":     # wiring test, not a real push
        return {"statusCode": 200, "body": "Trigger test event; no review run"}

    repo_name = record["eventSourceARN"].split(":")[-1]
    branch = record["codecommit"]["references"][0]["ref"].removeprefix("refs/heads/")
    source_commit = record["codecommit"]["references"][0]["commit"]

    pr_info = find_open_pr_for_branch(repo_name, branch)
    if not pr_info:
        return {"statusCode": 200, "body": f"No open PR found for {repo_name}#{branch}, skipping"}

    vm = context.step(launch_microvm())                    # checkpointed
    context.wait_for_condition(check=poll_until_running,
                               config=WaitForConditionConfig(wait_strategy=running_strategy, ...))
    token = context.step(create_auth_token(vm["microvm_id"]))

    callback = context.create_callback(name="kiro-review-complete")
    context.step(dispatch_review(vm["endpoint"], token,
                                 {**review_input, "callback_id": callback.callback_id}))

    review = callback.result()                             # <-- SUSPENDS, nothing billed
    context.step(terminate_microvm(vm["microvm_id"]))
    return {"statusCode": 200, "body": json.dumps(review)}
```

| Construct | Purpose |
|---|---|
| `@durable_execution` | makes the handler resumable; state is checkpointed |
| `context.step(...)` | records the result. On resume it is **replayed from state, not re-run** — so the VM is never launched twice |
| `context.wait_for_condition` | durable polling; each poll result is checkpointed |
| `context.create_callback()` | mints a `callback_id` an external system can complete |
| `callback.result()` | suspends until the callback arrives |

The `TriggerEventTest` guard matters: `test-repository-triggers` (step 3) sends a
synthetic event carrying a real branch ref. Without the guard, every wiring test would
launch a VM and run a full paid review.

The polling strategy is explicit about giving up:

```python
def running_strategy(state, iteration) -> WaitForConditionDecision:
    if state.get("state") == "RUNNING":
        return WaitForConditionDecision.stop_polling()
    if iteration >= 150:                        # 150 x 2s = 5 minutes
        raise TimeoutError(...)
    return WaitForConditionDecision.continue_waiting(Duration.from_seconds(2))
```

---

## 6. Step 3 — wire the trigger

```bash
cd module-2/trigger && ./wire-codecommit.sh
```

This is **two** independent things, and missing either breaks the pipeline silently.

### 3a — allow CodeCommit to invoke

```bash
aws lambda add-permission \
  --function-name "${FUNCTION_NAME}" \
  --qualifier live \
  --statement-id "codecommit-trigger" \
  --action lambda:InvokeFunction \
  --principal codecommit.amazonaws.com \
  --source-arn "${REPO_ARN}"
```

| Flag | Purpose |
|---|---|
| `--qualifier live` | attaches the permission to the **alias**, which is what the trigger targets |
| `--statement-id` | unique id; re-running errors, so the script treats "already exists" as success |
| `--principal` | only CodeCommit may invoke |
| `--source-arn` | only **this repository** may invoke — prevents a confused-deputy |

Verify it exists:

```bash
aws lambda get-policy --function-name durable-orchestrator --qualifier live
```

`ResourceNotFoundException` means no policy at all, so the trigger can never fire.
**This one command diagnoses the most common failure in the whole workshop.**

> Redeploying the stack **recreates** the function and therefore drops this policy.
> Re-run `wire-codecommit.sh` after any redeploy, even though CodeCommit still shows
> the trigger as present.

### 3b — register the trigger (merging, not overwriting)

`put-repository-triggers` **replaces the entire trigger set**, so a naive write would
delete module 3's trigger. The script merges by name:

```python
merged = [t for t in existing if t.get("name") != new["name"]] + [new]
```

```bash
aws codecommit put-repository-triggers \
  --repository-name "${REPO_NAME}" \
  --cli-input-json file:///tmp/repo-triggers.json
```

`--cli-input-json` feeds the whole request from a file, avoiding painful shell quoting
of nested JSON.

The trigger itself:

```json
{"name":"ai-review-trigger",
 "destinationArn":"arn:aws:lambda:...:function:durable-orchestrator:live",
 "branches":["feature/bad-code"],
 "events":["updateReference"]}
```

`updateReference` = commits pushed to an existing branch. (Module 3 also needs
`createReference`, because its first push *creates* the branch.)

### 3c — test without running a review

```bash
aws codecommit test-repository-triggers --repository-name "${REPO_NAME}" --triggers ...
# => {"successfulExecutions":["ai-review-trigger"],"failedExecutions":[]}
```

This proves the ARN resolves and the permission works. `failedExecutions` naming your
trigger almost always means the missing invoke permission.

---

## 7. Steps 4–5 — fire a real review

```bash
./prepare-review.sh
```

It configures git for CodeCommit using the **IAM role** rather than stored passwords:

```bash
git config --global credential.helper '!aws codecommit credential-helper $@'
git config --global credential.UseHttpPath true
```

> This writes a **global** helper. It will hijack HTTPS auth for other hosts such as
> GitHub, which signs requests with SigV4 and fails. Scope it per host instead:
> `git config --global credential.https://git-codecommit.us-east-1.amazonaws.com.helper '!aws codecommit credential-helper $@'`

It then writes `src/user_service.py` containing deliberate flaws — SQL injection via
string formatting, command injection through `shell=True`, hardcoded credentials, and
`eval()` — and stops. **You** make the commit, so the trigger is fired by a real push:

```bash
cd /tmp/code-review
git add src/user_service.py
git commit -m "Add user lookup and reporting helpers"
git push origin feature/bad-code
```

The script appends a changing timestamp comment so re-runs always produce a new commit;
an empty commit would push nothing and fire nothing.

---

## 8. What happens inside the reviewer VM

```python
self._json(202, {"status": "accepted", "callback_id": callback_id})   # answer FIRST
threading.Thread(target=run_review_and_callback, args=(body, callback_id),
                 daemon=True).start()                                # then work
```

Replying 202 before starting work is what frees the orchestrator to suspend.

Then it builds the diff and runs Claude:

```bash
git clone --no-checkout <codecommit-url> <work>   # metadata only; no working tree needed
git fetch --all
git diff <destination_commit>..<source_commit>    # exactly what the PR changes
claude -p --output-format text "<prompt with diff>"
```

`-p` is headless mode: prompt in, text out, no interactive session.

Because a single transient Bedrock error makes the CLI exit non-zero, the whole
invocation is retried with backoff:

```python
for attempt in range(1, max_attempts + 1):        # default 3
    claude = subprocess.run(["claude", "-p", "--output-format", "text", prompt], ...)
    if claude.returncode == 0 and claude.stdout.strip():
        review_text = claude.stdout.strip(); break
    last_error = (claude.stderr or claude.stdout or "").strip()[-800:]
    if attempt < max_attempts:
        time.sleep(2 ** attempt)                  # 2s, then 4s
```

Both streams are captured because `claude -p` often prints failures to **stdout**, not
stderr — logging only stderr yields a mystifying empty error.

Finally it posts the comment and completes the callback:

```bash
aws codecommit post-comment-for-pull-request \
  --pull-request-id <id> --repository-name <repo> \
  --before-commit-id <destination> --after-commit-id <source> --content <markdown>
```

```python
lambda_client.send_durable_execution_callback_success(
    CallbackId=callback_id, Result=json.dumps(result).encode())
```

Failures must also be reported, or the orchestrator sleeps until its timeout:

```python
lambda_client.send_durable_execution_callback_failure(
    CallbackId=callback_id,
    Error={"ErrorType": "ReviewFailed", "ErrorMessage": result.get("error")})
```

---

## 9. Verified result

The reviewer posted this on PR #1 (excerpt):

```
## 🤖 Claude AI Code Review (durable orchestration)
**[CRITICAL] SQL injection** — string-formatted query in get_user
**[CRITICAL] Command injection** — subprocess.call(cmd, shell=True)
**[CRITICAL] Hardcoded credentials** — DB_PASSWORD / API_TOKEN committed
**[CRITICAL] Arbitrary code execution** — eval(expression)
**[HIGH] Weak password hashing** — unsalted MD5
```

Durability is visible in the logs as several short invocations, not one long wait:

```
10:43:24  durationMs 1052
10:43:27  durationMs 1149
10:44:42  durationMs  604      <- resumed by the callback
```

Runtime reported as `python:3.13.DurableFunction.v40`.

---

## 10. IAM: which role does what

| Role | Used by | Must allow |
|---|---|---|
| build role | the image build in AWS | `s3:GetObject` on the artifacts zip |
| **VM execution role** | the reviewer VM | `bedrock:InvokeModel*`, CodeCommit read + comment, `lambda:SendDurableExecutionCallback{Success,Failure,Heartbeat}`, Logs |
| Lambda function role | the orchestrator | run/get/token/terminate MicroVMs, `iam:PassRole` for the VM role, read PRs |

> Confusing but true: `Module2ReviewerBuildRole-workshop` is the correct **execution**
> role here — it holds the Bedrock and callback permissions.
> `LambdaMicroVMExecutionRole-workshop` has neither. Trust the permissions, not the name.

Missing `SendDurableExecutionCallback*` is especially nasty: the review completes, the
comment appears, and the orchestrator still hangs until `ExecutionTimeout`.

---

## 11. Debugging

```bash
# 1 does the target exist, and may CodeCommit invoke it?
aws lambda get-function --function-name durable-orchestrator
aws lambda get-policy   --function-name durable-orchestrator --qualifier live

# 2 is the trigger on the right branch and event?
aws codecommit get-repository-triggers --repository-name "$REPO_NAME"

# 3 did the orchestrator run?
aws logs tail /aws/lambda/durable-orchestrator --since 15m --format short

# 4 did the VM start, and if it died, why?
aws lambda-microvms list-microvms
aws lambda-microvms get-microvm --microvm-identifier <id> --query '[state,stateReason]'

# 5 what did the reviewer say? (binary TLS noise -> strings)
aws logs tail /aws/lambda-microvms/mvm-claude-reviewer-async --since 15m | strings | grep -a claude

# 6 did the comment land?
aws codecommit get-comments-for-pull-request --pull-request-id 1
```

| Symptom | Likely cause |
|---|---|
| push does nothing, no logs | function missing, or no invoke permission (check 1) |
| log says `No open PR found ... skipping` | no **open** PR whose source branch matches |
| log says `Trigger test event` | that was `test-repository-triggers`, not a push |
| orchestrator starts then hangs forever | VM never called back — check its callback IAM permissions |
| `claude failed after 3 attempts` | Bedrock model not enabled for the role, or wrong model id |
| VM dies immediately | lifecycle hook returned non-200 — read `stateReason` |

---

## 12. Security notes

- The reviewer **executes no repository code** — it only diffs and reads. That is a
  deliberate limit; running a PR's build in an AI reviewer would be far riskier.
- **No API key exists in the image.** Bedrock access comes from the VM execution role,
  so there is no secret to leak in image history.
- The review prompt embeds an untrusted diff. Treat model output as **advisory**: a
  hostile PR can attempt prompt injection to influence the review text. Never wire
  such output directly into an automated merge decision.
- The sample `user_service.py` is intentionally vulnerable. Keep it out of anything
  that scans or deploys real code — and note its fake API token is deliberately *not*
  vendor-shaped, because a `sk_live_...` literal trips GitHub push protection.

**Next:** [MODULE-3.md](MODULE-3.md) — same trigger mechanics, but running a real build
and test pipeline instead of an AI review.
