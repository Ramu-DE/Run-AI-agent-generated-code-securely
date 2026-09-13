# CI/CD in this workshop

Two pipelines, both fired by a **CodeCommit repository trigger**, both running their
work inside a throwaway MicroVM:

- **Module 2** — AI code review on a PR, orchestrated by a *durable* Lambda.
- **Module 3** — a real build/test pipeline on an ephemeral runner.

## CI/CD in one paragraph

**Continuous Integration** means every push is automatically built and tested, so
breakage is caught in minutes rather than at release. **Continuous Delivery** extends
that to an automated path to production. The pieces are always the same: a *trigger*
(something changed), a *runner* (isolated compute), *steps* (build, test, review), and
*feedback* (status where the developer is looking — here, a PR comment).

The MicroVM angle: the runner is created per push and destroyed after, so there is no
persistent build fleet to patch and no state leaking between builds.

## The pipeline chain

Both modules follow the same four steps, and **order matters** — most failures come
from running them out of order:

```
1. build-image.sh    -> publish the MicroVM image     (persists <X>_IMAGE_ARN)
2. deploy.sh         -> sam build + sam deploy        (creates the :live alias)
3. wire-codecommit.sh-> add-permission + put-triggers (needs step 2 to exist)
4. prepare-*.sh + git push -> fires the trigger
```

Step 3 depends on step 2 having produced a function and an alias. Skip step 2 and the
pipeline is silently dead — that is exactly the failure documented in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Trigger wiring: two things, not one

A CodeCommit trigger needs **both** a registered trigger *and* a Lambda resource
policy allowing CodeCommit to invoke. Miss the permission and pushes do nothing.

```bash
# 1. let CodeCommit invoke the alias
aws lambda add-permission \
  --function-name durable-orchestrator --qualifier live \
  --statement-id codecommit-trigger \
  --action lambda:InvokeFunction \
  --principal codecommit.amazonaws.com \
  --source-arn "arn:aws:codecommit:$AWS_REGION:$AWS_ACCOUNTID:$REPO_NAME"

# 2. register the trigger
aws codecommit put-repository-triggers --repository-name "$REPO_NAME" \
  --cli-input-json file:///tmp/repo-triggers.json
```

Verify with:

```bash
aws lambda get-policy --function-name durable-orchestrator --qualifier live
```

`ResourceNotFoundException` here means **no policy exists at all** and the trigger can
never fire. It is the fastest way to confirm this class of failure.

### `put-repository-triggers` replaces the whole set

The API is a full overwrite, not an append. A naive write from module 3 would delete
module 2's trigger. Both wiring scripts therefore merge by name:

```python
merged = [t for t in existing if t.get("name") != new["name"]] + [new]
```

Result — both triggers coexisting, scoped to different branches:

```json
[{"name":"ai-review-trigger","branches":["feature/bad-code"],"events":["updateReference"]},
 {"name":"ci-runner-trigger","branches":["feature/ci-pipeline"],"events":["createReference","updateReference"]}]
```

### `createReference` vs `updateReference`

- `updateReference` — commits pushed to an **existing** branch.
- `createReference` — the branch is **created**.

Module 3 registers both, because its first push *creates* `feature/ci-pipeline`. With
only `updateReference`, that first push fires nothing — a genuinely confusing
"my pipeline never ran" symptom.

### Test the wiring without running a build

```bash
aws codecommit test-repository-triggers --repository-name "$REPO_NAME" --triggers ...
# => {"successfulExecutions":["ai-review-trigger"],"failedExecutions":[]}
```

This sends a **synthetic** event with `eventName: "TriggerEventTest"`. Both
orchestrators guard against it, so a wiring test does not launch a VM or post a review:

```python
if record.get("eventName") == "TriggerEventTest":
    return {"statusCode": 200, "body": "Trigger test event; no review run"}
```

Without that guard, every wiring test would cost a full AI review.

### Bind triggers to an alias, not a version

`AutoPublishAlias: live` in the SAM template publishes a new numbered version on each
deploy and repoints `live` at it. The trigger targets
`...:function:durable-orchestrator:live`, so redeploying never requires rewiring.

> Redeploying **recreates the resource policy**. After a stack is deleted and
> recreated, re-run `wire-codecommit.sh` even though the trigger still looks correct
> in CodeCommit.

## Module 2: durable orchestration and the callback pattern

An AI review takes minutes. Three ways to wait, only one of which is sensible:

| Approach | Problem |
|---|---|
| Lambda blocks on the response | pays for idle time, dies at the 15-minute cap |
| Poll from a second function | extra moving parts, wasted invocations |
| **Durable execution + callback** | suspends with **zero compute billed** |

The handler reads as straight-line code:

```python
@durable_execution
def lambda_handler(event, context: DurableContext) -> dict:
    ...
    vm = context.step(launch_microvm())
    context.wait_for_condition(check=poll_until_running, config=...)
    token = context.step(create_auth_token(vm["microvm_id"]))

    callback = context.create_callback(name="kiro-review-complete")
    context.step(dispatch_review(vm["endpoint"], token,
                                {**review_input, "callback_id": callback.callback_id}))

    review = callback.result()          # <-- suspends here, nothing billed
    context.step(terminate_microvm(vm["microvm_id"]))
    return {"statusCode": 200, "body": json.dumps(review)}
```

The VM accepts the job, returns **202 immediately**, works on a background thread, and
wakes the orchestrator when finished:

```python
self._json(202, {"status": "accepted", "callback_id": callback_id})
threading.Thread(target=run_review_and_callback, args=(body, callback_id), daemon=True).start()
...
lambda_client.send_durable_execution_callback_success(
    CallbackId=callback_id, Result=json.dumps(result).encode())
```

Each `context.step(...)` is **checkpointed**, so a resumed execution replays completed
steps from state instead of re-running them — the VM is not launched twice. In
CloudWatch this appears as several short invocations rather than one long one:

```
10:43:24 platform.start ... durationMs 1052
10:43:27 platform.start ... durationMs 1149
10:44:42 platform.start ... durationMs 604
```

Requirements that bite:
- **Python 3.13** at build *and* runtime (`BuildMethod: python3.13`, `Runtime: python3.13`).
  Observed runtime: `python:3.13.DurableFunction.v40`.
- `boto3>=1.43.0` for the `SendDurableExecutionCallback*` APIs.
- The VM's execution role needs `lambda:SendDurableExecutionCallbackSuccess`,
  `...Failure`, and `...Heartbeat`, or the orchestrator sleeps until it times out.
- `DurableConfig` is immutable; changing it replaces the function and drops in-flight
  executions.

Failures must be reported too, or the orchestrator hangs:

```python
lambda_client.send_durable_execution_callback_failure(
    CallbackId=callback_id,
    Error={"ErrorType": "ReviewFailed", "ErrorMessage": result.get("error")})
```

The reviewer also retries Claude up to three times with exponential backoff, because a
single transient Bedrock error makes the CLI exit non-zero.

## Module 3: ephemeral runner

No durable execution needed — the orchestrator hands off and exits, and the runner is
autonomous:

1. resolve the open PR for the pushed branch, or **open one** if none exists
2. `run_microvm` with a short idle policy and `autoResumeEnabled: false`
3. wait for `RUNNING`, mint a token for port 9000
4. `POST /run` with the job → runner replies 202
5. orchestrator returns; the runner clones, builds, comments, then self-terminates

The pipeline lives **in the repo**, not in the platform:

```bash
# ci/steps.sh
set -e
echo "== compile =="
python -m compileall -q src
echo "== test =="
python -m unittest discover -s tests -p 'test_*.py'
```

`set -e` is what makes the build fail on the first failing step. The runner captures
the exit code and reports it:

```python
proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=600)
return {"passed": proc.returncode == 0, "exit_code": proc.returncode,
        "output": (proc.stdout + proc.stderr)[-4000:]}
```

Verified feedback on the auto-opened PR #2:

```
## CI build passed (exit code 0)
Ran on an ephemeral Lambda MicroVM runner, commit `5554a2a8e1`.
== compile ==
== test ==
Ran 1 test in 0.000s
OK
```

If `ci/steps.sh` is absent the runner falls back to `compileall` plus an optional
`pip install -r requirements.txt`, so a repo with no pipeline still gets a signal.

## Design points worth stealing

**Convention over configuration.** No pipeline YAML — a repo defines `ci/steps.sh` and
the runner executes it. The platform stays out of the way.

**Fail visibly, at the PR.** Both modules post results as PR comments. A pipeline whose
output nobody reads is not a pipeline.

**Isolate per job.** Each build gets a fresh kernel and filesystem, so no build can
poison the next through leftover state or a mutated global cache.

**Least privilege per stage.** The build role can only read the artifacts bucket; the
runner's execution role can only reach CodeCommit and Bedrock. Compromising a build
does not hand over the account.

**Bind to aliases, not versions**, so deploys don't require rewiring.

## Debugging a pipeline that "does nothing"

Work down this list; it resolves nearly every case:

```bash
# 1. does the function exist at all?
aws lambda get-function --function-name durable-orchestrator

# 2. is CodeCommit allowed to invoke the alias?  (ResourceNotFoundException = no)
aws lambda get-policy --function-name durable-orchestrator --qualifier live

# 3. is the trigger registered on the right branch and events?
aws codecommit get-repository-triggers --repository-name "$REPO_NAME"

# 4. did it fire?
aws logs tail /aws/lambda/durable-orchestrator --since 15m --format short

# 5. did the VM start, and if it died, why?
aws lambda-microvms get-microvm --microvm-identifier <id> --query '[state,stateReason]'

# 6. what did the app inside the VM say?  (logs contain binary — use strings)
aws logs tail /aws/lambda-microvms/<image-name> --since 15m | strings | grep -a POST
```

One more silent-skip trap: both orchestrators do nothing unless an **open PR** exists
whose source branch matches the push.

```python
if not pr_info:
    return {"statusCode": 200, "body": f"No open PR found for {repo}#{branch}, skipping"}
```

A `200` with `"skipping"` looks like success in metrics. Module 3 avoids the trap by
opening a PR itself; module 2 relies on one already being open.
