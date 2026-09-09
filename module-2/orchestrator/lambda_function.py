"""Durable orchestrator for the MicroVM Kiro code review pipeline.

This handler fires from a CodeCommit repository trigger on push to
the source branch of an open pull request. It:

1. Parses the CodeCommit trigger event to get repo, branch, source commit
2. Looks up the open PR whose source branch matches the pushed branch
3. Launches an ephemeral MicroVM from the callback-enabled Kiro image
4. Waits for the MicroVM to reach RUNNING (via wait_for_condition)
5. Generates an auth token
6. Creates a callback ID and fire-and-forget POSTs to /review with it
7. Suspends on callback.result() until the MicroVM posts back
8. Terminates the MicroVM and returns the review payload

Downloaded from the workshop assets at the start of Module 2's
durable-orchestration lab (Step 4).
"""

import json
import os
import boto3
import urllib.request

from aws_durable_execution_sdk_python import (
    DurableContext,
    StepContext,
    durable_execution,
    durable_step,
)
from aws_durable_execution_sdk_python.config import Duration
from aws_durable_execution_sdk_python.waits import (
    WaitForConditionConfig,
    WaitForConditionDecision,
)

REGION = os.environ["MVM_REGION"]
IMAGE_ARN = os.environ["MVM_IMAGE_ARN"]
EXECUTION_ROLE = os.environ["MVM_EXECUTION_ROLE_ARN"]

microvms = boto3.client("lambda-microvms", region_name=REGION)
codecommit = boto3.client("codecommit", region_name=REGION)


@durable_step
def launch_microvm(ctx: StepContext) -> dict:
    resp = microvms.run_microvm(
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
        idlePolicy={
            "maxIdleDurationSeconds": 300,
            "suspendedDurationSeconds": 60,
            "autoResumeEnabled": False,
        },
    )
    return {"microvm_id": resp["microvmId"], "endpoint": resp["endpoint"]}


@durable_step
def create_auth_token(ctx: StepContext, microvm_id: str) -> str:
    resp = microvms.create_microvm_auth_token(
        microvmIdentifier=microvm_id,
        expirationInMinutes=60,
        allowedPorts=[{"port": 9000}],
    )
    return resp["authToken"]["X-aws-proxy-auth"]


def poll_until_running(state: dict, ctx) -> dict:
    """Check callable for wait_for_condition. Runs implicitly as a durable step
    (its return value is checkpointed on each poll, and the SDK replays from the
    latest state on interruption).

    Takes the current state (dict with microvm_id + state) and returns the updated
    state after a single GetMicrovm call.
    """
    resp = microvms.get_microvm(microvmIdentifier=state["microvm_id"])
    return {**state, "state": resp["state"]}


def running_strategy(state: dict, iteration: int) -> WaitForConditionDecision:
    """Wait strategy: stop when state is RUNNING, otherwise poll every 2s up to ~5 min."""
    if state.get("state") == "RUNNING":
        return WaitForConditionDecision.stop_polling()
    if iteration >= 150:  # 150 polls x 2s = 5 min
        raise TimeoutError(
            f"MicroVM {state.get('microvm_id')} did not reach RUNNING after 5 min"
        )
    return WaitForConditionDecision.continue_waiting(Duration.from_seconds(2))


@durable_step
def dispatch_review(
    ctx: StepContext,
    endpoint: str,
    auth_token: str,
    payload: dict,
) -> None:
    """Fire-and-forget POST to the MicroVM /review endpoint.

    The MicroVM returns 202 immediately, runs Kiro asynchronously,
    and later signals completion by calling SendDurableExecutionCallback.
    """
    req = urllib.request.Request(
        f"https://{endpoint}/review",
        method="POST",
        headers={
            "X-aws-proxy-auth": auth_token,
            "X-aws-proxy-port": "9000",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload).encode(),
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status != 202:
            raise RuntimeError(f"MicroVM did not accept the review: {r.status}")


@durable_step
def terminate_microvm(ctx: StepContext, microvm_id: str) -> None:
    microvms.terminate_microvm(microvmIdentifier=microvm_id)


def find_open_pr_for_branch(repo_name: str, branch: str) -> dict | None:
    """Return {pr_id, destination_commit} for the open PR whose source
    branch is `branch`, or None if no such PR exists.
    """
    prs = codecommit.list_pull_requests(
        repositoryName=repo_name,
        pullRequestStatus="OPEN",
    )
    for pr_id in prs.get("pullRequestIds", []):
        pr = codecommit.get_pull_request(pullRequestId=pr_id)
        for target in pr["pullRequest"]["pullRequestTargets"]:
            if target["sourceReference"] == f"refs/heads/{branch}":
                return {
                    "pr_id": pr_id,
                    "destination_commit": target["destinationCommit"],
                }
    return None


@durable_execution
def lambda_handler(event: dict, context: DurableContext) -> dict:
    # CodeCommit repository triggers deliver events in this shape:
    # {
    #   "Records": [{
    #     "eventSourceARN": "arn:aws:codecommit:region:account:repo-name",
    #     "codecommit": {"references": [{
    #       "commit": "<sha>",
    #       "ref": "refs/heads/<branch>"
    #     }]},
    #     ...
    #   }]
    # }
    record = event["Records"][0]

    # CodeCommit's TestRepositoryTriggers API (used by wire-codecommit.sh to
    # validate the wiring) sends a synthetic event with eventName
    # "TriggerEventTest". It carries the real branch ref, so without this
    # guard the orchestrator would launch a MicroVM and run a full review
    # just to test the trigger. Only react to real reference changes.
    if record.get("eventName") == "TriggerEventTest":
        return {"statusCode": 200, "body": "Trigger test event; no review run"}

    repo_name = record["eventSourceARN"].split(":")[-1]
    ref = record["codecommit"]["references"][0]["ref"]
    branch = ref.removeprefix("refs/heads/")
    source_commit = record["codecommit"]["references"][0]["commit"]

    pr_info = find_open_pr_for_branch(repo_name, branch)
    if not pr_info:
        return {
            "statusCode": 200,
            "body": f"No open PR found for {repo_name}#{branch}, skipping",
        }

    review_input = {
        "repo_name": repo_name,
        "pr_id": pr_info["pr_id"],
        "source_commit": source_commit,
        "destination_commit": pr_info["destination_commit"],
        "region": REGION,
    }

    vm = context.step(launch_microvm())

    context.wait_for_condition(
        check=poll_until_running,
        config=WaitForConditionConfig(
            wait_strategy=running_strategy,
            initial_state={"microvm_id": vm["microvm_id"], "state": "unknown"},
        ),
    )

    token = context.step(create_auth_token(vm["microvm_id"]))

    # Create a callback the MicroVM will use to signal completion.
    callback = context.create_callback(name="kiro-review-complete")

    # Fire-and-forget POST including the callback_id. The MicroVM returns
    # 202 and runs Kiro in a background thread.
    context.step(
        dispatch_review(
            vm["endpoint"],
            token,
            {**review_input, "callback_id": callback.callback_id},
        )
    )

    # Suspend until the MicroVM calls SendDurableExecutionCallbackSuccess
    # (or SendDurableExecutionCallbackFailure). No compute is billed
    # during the wait, which can span the entire Kiro run.
    review = callback.result()

    context.step(terminate_microvm(vm["microvm_id"]))

    return {"statusCode": 200, "body": json.dumps(review)}
