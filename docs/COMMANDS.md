# Command reference

Every command used across the four modules, with the purpose of each flag. For the
narrative order in which they are used, see the per-module walkthroughs in
[modules/](modules/README.md).

---

## MicroVM images

### `create-microvm-image` — first build of an image

```bash
aws lambda-microvms create-microvm-image \
  --name mvm-code-execution-sandbox \
  --code-artifact "uri=s3://$ARTIFACTS_BUCKET/deployments/app.zip" \
  --base-image-arn "arn:aws:lambda:$AWS_REGION:aws:microvm-image:al2023-1" \
  --build-role-arn "arn:aws:iam::$AWS_ACCOUNTID:role/LambdaMicroVMBuildRole-workshop" \
  --hooks '{...}' \
  --environment-variables "KEY=value" \
  --egress-network-connectors "arn:...:aws-network-connector:INTERNET_EGRESS" \
  --resources '[{"minimumMemoryInMiB":2048}]' \
  --logging '{"cloudWatch":{"logGroup":"/aws/lambda-microvms/<name>"}}'
```

| Flag | Purpose |
|---|---|
| `--name` | logical image name; becomes part of the image ARN |
| `--code-artifact` | S3 location of the zip **build context**, in `uri=` form |
| `--base-image-arn` | AWS-managed base providing kernel + init (`al2023-1`) |
| `--build-role-arn` | role AWS assumes **to build**; needs `s3:GetObject` on the zip |
| `--hooks` | which lifecycle hooks fire, on which port, with what timeouts |
| `--environment-variables` | baked into the image. **Config only — never secrets** |
| `--egress-network-connectors` | outbound internet. Omit to sandbox harder |
| `--resources` | `minimumMemoryInMiB` — memory floor for the VM |
| `--logging` | CloudWatch group for build output *and* app stdout |

Returns immediately with an `imageArn`; the build is asynchronous. Creates version `1.0`.

### `update-microvm-image` — publish a new version

Same flags as `create`, but `--image-identifier` instead of `--name`, plus optional
`--description`. It **publishes a new version** (`2.0`, `3.0`, …) rather than mutating
the existing one, so rollback is just launching the previous number.

```bash
aws lambda-microvms update-microvm-image \
  --image-identifier "$IMAGE_ARN" \
  --code-artifact "uri=s3://$ARTIFACTS_BUCKET/deployments/app-new.zip" \
  --base-image-arn "..." --build-role-arn "..." \
  --description "what changed" --hooks '{...}' --resources '[...]' --logging '{...}'
```

### Inspecting images

```bash
aws lambda-microvms list-microvm-images                       # all images + latest version
aws lambda-microvms get-microvm-image --image-identifier <arn>  # state, latestActiveImageVersion
aws lambda-microvms get-microvm-image-version \
  --image-identifier <arn> --image-version 3.0 \
  --query state --output text                                 # IN_PROGRESS|SUCCESSFUL|FAILED
aws lambda-microvms list-microvm-image-versions --image-identifier <arn>
aws lambda-microvms list-microvm-image-builds
aws lambda-microvms get-microvm-image-build --image-identifier <arn>
```

| Flag | Purpose |
|---|---|
| `--query state` | JMESPath — print just the field you need |
| `--output text` | bare string, safe for `if`/`case` in shell |

Wait loop used by every `build-image.sh`:

```bash
while true; do
  STATE=$(aws lambda-microvms get-microvm-image-version \
    --image-identifier "$IMAGE_ARN" --image-version 1.0 \
    --query 'state' --output text 2>/dev/null || echo "PENDING")
  case "$STATE" in
    SUCCESSFUL) break ;;
    FAILED)     echo "build FAILED"; exit 1 ;;
    *)          sleep 10 ;;
  esac
done
```

The `|| echo "PENDING"` matters: the version does not exist for the first second or two,
and without it `set -e` aborts the script.

### Deleting

```bash
aws lambda-microvms delete-microvm-image-version --image-identifier <arn> --image-version 1.0
aws lambda-microvms delete-microvm-image --image-identifier <arn>
```

---

## Running MicroVMs

### `run-microvm` — launch

```bash
aws lambda-microvms run-microvm \
  --image-identifier "$IMAGE_ARN" \
  --image-version 3.0 \
  --ingress-network-connectors "arn:...:aws-network-connector:HTTP_INGRESS" \
  --egress-network-connectors  "arn:...:aws-network-connector:INTERNET_EGRESS" \
  --execution-role-arn "arn:aws:iam::$AWS_ACCOUNTID:role/<ExecutionRole>" \
  --idle-policy '{"maxIdleDurationSeconds":600,"suspendedDurationSeconds":300,"autoResumeEnabled":true}'
```

| Flag | Purpose |
|---|---|
| `--image-version` | **which version boots.** Launching `1.0` after publishing `3.0` silently runs old code |
| `--ingress-network-connectors` | `HTTP_INGRESS` gives a public HTTPS endpoint. Without it, nothing reaches in |
| `--egress-network-connectors` | outbound access for the running VM |
| `--execution-role-arn` | role the **running VM** assumes; credentials appear inside automatically |
| `--idle-policy` | automates the VM's whole lifetime |

Returns `microvmId`, `endpoint`, `state` (initially `PENDING`), and
`maximumDurationInSeconds` (28800 = 8 h hard cap).

Idle policy fields:

| Field | Meaning |
|---|---|
| `maxIdleDurationSeconds` | no traffic this long → `SUSPENDED` |
| `suspendedDurationSeconds` | suspended this long → `TERMINATED` |
| `autoResumeEnabled` | `true` = next request resumes; `false` = one-shot VM |

### Inspecting and controlling

```bash
aws lambda-microvms get-microvm --microvm-identifier <id>                     # full state
aws lambda-microvms get-microvm --microvm-identifier <id> --query '[state,stateReason]'
aws lambda-microvms list-microvms
aws lambda-microvms suspend-microvm   --microvm-identifier <id>
aws lambda-microvms resume-microvm    --microvm-identifier <id>
aws lambda-microvms terminate-microvm --microvm-identifier <id>
```

**`stateReason` is the most useful field in this API.** When a VM dies unexpectedly it
names the failing hook and its HTTP status:

```
"Run lifecycle hook returned HTTP status 404. Please check your hook endpoint ..."
```

States: `PENDING → RUNNING → SUSPENDED → TERMINATED`.

### `create-microvm-auth-token` — authenticate to a VM

```bash
TOKEN=$(aws lambda-microvms create-microvm-auth-token \
  --microvm-identifier "$VM" \
  --expiration-in-minutes 60 \
  --allowed-ports '[{"port":8080}]' \
  --query 'authToken."X-aws-proxy-auth"' --output text)
```

| Flag | Purpose |
|---|---|
| `--expiration-in-minutes` | short TTL, so a leaked token expires on its own |
| `--allowed-ports` | **ports this token may reach.** A token for 8080 cannot touch 9000 |
| `--query 'authToken."X-aws-proxy-auth"'` | extract the header value; quoted because the key contains `-` |

Then call the VM:

```bash
curl -H "X-aws-proxy-auth: $TOKEN" -H "X-aws-proxy-port: 8080" "https://$ENDPOINT/path"
```

| Header | Purpose |
|---|---|
| `X-aws-proxy-auth` | the signed token |
| `X-aws-proxy-port` | **which port inside the VM** to route to; must be in `--allowed-ports` |

Omitting `X-aws-proxy-port` is a common mistake — the proxy needs the destination port.

Also available: `create-microvm-shell-auth-token` (interactive shell access),
`tag-resource`, `untag-resource`, `list-tags`.

---

## SAM: build and deploy

```bash
sam build
sam deploy --region "$AWS_REGION" \
  --parameter-overrides "ImageArn=$IMAGE_ARN" "MvmExecutionRoleArn=$MVM_EXECUTION_ROLE_ARN"
```

| Command / flag | Purpose |
|---|---|
| `sam build` | pip-installs `requirements.txt` and stages code into `.aws-sam/build` |
| `--parameter-overrides` | injects template `Parameters` (image ARN, VM role) |
| `--region` | target region |

`samconfig.toml` supplies the rest so you never retype it:

```toml
[default.global.parameters]
stack_name = "durable-orchestrator"

[default.deploy.parameters]
resolve_s3 = true                 # auto-create/use a bucket for the package
capabilities = "CAPABILITY_IAM"   # permission to create IAM resources
confirm_changeset = false         # non-interactive
fail_on_empty_changeset = false   # a no-op redeploy is success, not an error
```

`BuildMethod: python3.13` in the template resolves against **`python3.13` on `PATH`**,
not the default `python3`. A default `python3` of 3.9 is fine as long as `python3.13`
also exists.

Read stack outputs:

```bash
aws cloudformation describe-stacks --stack-name <stack> \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
```

---

## CI/CD wiring

### Allow CodeCommit to invoke the function

```bash
aws lambda add-permission \
  --function-name "$FUNCTION_NAME" \
  --qualifier live \
  --statement-id codecommit-trigger \
  --action lambda:InvokeFunction \
  --principal codecommit.amazonaws.com \
  --source-arn "arn:aws:codecommit:$AWS_REGION:$AWS_ACCOUNTID:$REPO_NAME"
```

| Flag | Purpose |
|---|---|
| `--qualifier live` | attach to the **alias** the trigger targets |
| `--statement-id` | unique id; re-running errors, so scripts treat "exists" as success |
| `--action` | the single permitted action |
| `--principal` | only CodeCommit may invoke |
| `--source-arn` | only **this repo** may invoke — prevents a confused-deputy |

Verify — the single best diagnostic in the workshop:

```bash
aws lambda get-policy --function-name durable-orchestrator --qualifier live
```

`ResourceNotFoundException` = no policy at all, so the trigger can never fire.

> Redeploying the stack recreates the function and **drops this policy**. Re-run the
> wiring script after any redeploy, even though the trigger still looks fine.

### Register the trigger

```bash
aws codecommit get-repository-triggers --repository-name "$REPO_NAME"     # read first!
aws codecommit put-repository-triggers --repository-name "$REPO_NAME" \
  --cli-input-json file:///tmp/repo-triggers.json
```

`put-repository-triggers` **replaces the entire trigger set**, so always read-merge-write:

```python
merged = [t for t in existing if t.get("name") != new["name"]] + [new]
```

`--cli-input-json file://...` feeds the whole request from a file, avoiding painful shell
quoting of nested JSON. Note the triple slash in `file:///tmp/...` — `file://` plus the
absolute path `/tmp/...`.

Trigger shape:

```json
{"name":"ai-review-trigger",
 "destinationArn":"arn:aws:lambda:...:function:durable-orchestrator:live",
 "branches":["feature/bad-code"],
 "events":["updateReference"]}
```

| Event | Fires when |
|---|---|
| `updateReference` | commits pushed to an **existing** branch |
| `createReference` | the branch is **created** — needed for a first push to a new branch |

### Test the wiring without doing the work

```bash
aws codecommit test-repository-triggers --repository-name "$REPO_NAME" \
  --triggers "name=<n>,destinationArn=<arn>,branches=<b>,events=updateReference"
# => {"successfulExecutions":["ai-review-trigger"],"failedExecutions":[]}
```

Sends a **synthetic** event with `eventName: "TriggerEventTest"`. Both orchestrators
guard against it, so testing does not launch a VM:

```python
if record.get("eventName") == "TriggerEventTest":
    return {"statusCode": 200, "body": "Trigger test event; no review run"}
```

---

## Git against CodeCommit

```bash
git config --global credential.helper '!aws codecommit credential-helper $@'
git config --global credential.UseHttpPath true
```

Authenticates using the current **IAM role** — no stored passwords. `UseHttpPath` is
required because CodeCommit's helper needs the repository path to sign the request.

> **This is a global setting and will break other hosts.** It signs *all* HTTPS git
> traffic with SigV4, so GitHub pushes fail. Scope it per host instead:
>
> ```bash
> git config --global credential.https://git-codecommit.us-east-1.amazonaws.com.helper \
>   '!aws codecommit credential-helper $@'
> git config --global credential.https://git-codecommit.us-east-1.amazonaws.com.UseHttpPath true
> ```

Other repository commands:

```bash
aws codecommit get-repository    --repository-name "$REPO_NAME" \
  --query "repositoryMetadata.cloneUrlHttp" --output text
aws codecommit list-repositories
aws codecommit list-branches    --repository-name "$REPO_NAME"
aws codecommit list-pull-requests --repository-name "$REPO_NAME" --pull-request-status OPEN
aws codecommit get-pull-request --pull-request-id 1
aws codecommit get-comments-for-pull-request --pull-request-id 1
aws codecommit post-comment-for-pull-request \
  --pull-request-id <id> --repository-name <repo> \
  --before-commit-id <destination> --after-commit-id <source> --content <markdown>
```

`--before-commit-id` / `--after-commit-id` anchor the comment to a specific diff range,
which is what makes it appear on the right revision of the PR.

---

## Logs and diagnosis

```bash
# Lambda orchestrators
aws logs tail /aws/lambda/durable-orchestrator   --since 15m --format short
aws logs tail /aws/lambda/ci-runner-orchestrator --since 15m --format short
aws logs tail /aws/lambda/saas-control-plane     --since 15m --format short

# Inside the MicroVMs (build output AND app stdout)
aws logs tail /aws/lambda-microvms/<image-name> --since 15m | strings | grep -a POST
```

| Part | Purpose |
|---|---|
| `--since 15m` | relative window instead of timestamps |
| `--format short` | drops the noisy stream prefix |
| `strings` | MicroVM logs carry **binary TLS handshake noise**; without this, `grep` reports "binary file matches" and prints nothing |
| `grep -a` | treat input as text regardless |

The TLS noise is expected: the ingress proxy speaks TLS to your port, so a plain HTTP
handler logs occasional `400 Bad request version` lines. Harmless.

---

## Environment variables the scripts expect

| Variable | Used for |
|---|---|
| `AWS_REGION` | every call |
| `AWS_ACCOUNTID` | building ARNs |
| `ARTIFACTS_BUCKET` | where build zips are uploaded |
| `FUNCTION_NAME` | module 2 orchestrator name |
| `MVM_EXECUTION_ROLE_ARN` | role the VMs run as |
| `MODULE2_REVIEWER_BUILD_ROLE_ARN` | role AWS builds images with |
| `IMAGE_ARN` | module 2 image, persisted to `/etc/profile.d/mvm-image-arn.sh` |
| `CI_RUNNER_IMAGE_ARN` | module 3 image, `/etc/profile.d/ci-runner-image.sh` |
| `SAAS_IMAGE_ARN` | module 4 image, `/etc/profile.d/saas-image.sh` |

Each `build-image.sh` writes its ARN into `/etc/profile.d/`, which every new login shell
sources, so a `deploy.sh` run in a different terminal still finds it. The deploy scripts
also re-source the file directly, covering shells that were already open.

Scripts assert their inputs up front, which is why a missing variable produces a clear
message instead of a confusing API error:

```bash
: "${AWS_REGION:?AWS_REGION must be set}"
```

---

## Preflight check

```bash
aws lambda-microvms help >/dev/null 2>&1 && echo "microvms API present" || echo "CLI too old"
sam --version
python3.13 --version          # required by the SAM templates
command -v zip jq git
aws sts get-caller-identity   # confirm the right account and role
```

`docker` is intentionally absent from this list — image builds happen inside AWS. See
[DOCKER.md](DOCKER.md).
