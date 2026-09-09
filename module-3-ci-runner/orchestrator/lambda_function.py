"""Ephemeral CI runner orchestrator for the Module 3 lab.

Invoked by a CodeCommit repository trigger on each push to a PR branch.
Finds the open pull request for the pushed branch, launches a single-use
runner MicroVM, waits for it to reach RUNNING, mints an auth token, and
hands the job to the runner over HTTP. Then it returns; the runner is
autonomous from that point and self-terminates via its idle policy.
"""

import json
import logging
import os
import time
import urllib.request

import boto3

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

REGION = os.environ["MVM_REGION"]
IMAGE_ARN = os.environ["MVM_IMAGE_ARN"]
EXECUTION_ROLE = os.environ["MVM_EXECUTION_ROLE_ARN"]
DEST_BRANCH = os.environ.get("CI_PR_DESTINATION_BRANCH", "main")

microvms = boto3.client("lambda-microvms", region_name=REGION)
codecommit = boto3.client("codecommit", region_name=REGION)


def find_open_pr_for_branch(repo_name: str, branch: str) -> dict | None:
    """Return {pr_id, destination_commit} for the open PR whose source
    branch is `branch`, or None if there is no such PR."""
    prs = codecommit.list_pull_requests(
        repositoryName=repo_name, pullRequestStatus="OPEN",
    )
    for pr_id in prs.get("pullRequestIds", []):
        pr = codecommit.get_pull_request(pullRequestId=pr_id)
        for target in pr["pullRequest"]["pullRequestTargets"]:
            if target["sourceReference"] == f"refs/heads/{branch}":
                return {"pr_id": pr_id, "destination_commit": target["destinationCommit"]}
    return None


def open_pull_request(repo_name: str, branch: str) -> dict | None:
    """Open a PR from `branch` into DEST_BRANCH so the runner has a pull request
    to report status on, and return {pr_id, destination_commit}. Returns None if
    a PR cannot be opened (for example the branch has no difference from the
    destination). Safe under a create race: it re-reads the open PRs and returns
    whichever one won.
    """
    try:
        dest = codecommit.get_branch(repositoryName=repo_name, branchName=DEST_BRANCH)
    except codecommit.exceptions.BranchDoesNotExistException:
        logger.info(f"destination branch {DEST_BRANCH} missing; cannot open a PR")
        return None
    destination_commit = dest["branch"]["commitId"]
    try:
        resp = codecommit.create_pull_request(
            title=f"CI pipeline: {branch}",
            description="Opened automatically by the Module 3 CI runner lab.",
            targets=[{
                "repositoryName": repo_name,
                "sourceReference": f"refs/heads/{branch}",
                "destinationReference": f"refs/heads/{DEST_BRANCH}",
            }],
        )
        pr_id = resp["pullRequest"]["pullRequestId"]
        logger.info(f"opened PR {pr_id} for {repo_name}#{branch} -> {DEST_BRANCH}")
        return {"pr_id": pr_id, "destination_commit": destination_commit}
    except Exception as e:
        # Most often: no difference yet between branch and destination, or a
        # concurrent invocation already opened one. Re-check for an open PR.
        logger.info(f"create PR failed for {repo_name}#{branch}: {e}; re-checking")
        return find_open_pr_for_branch(repo_name, branch)


def wait_until_running(microvm_id: str, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = microvms.get_microvm(microvmIdentifier=microvm_id)["state"]
        if state == "RUNNING":
            return
        if state in ("FAILED", "TERMINATED"):
            raise RuntimeError(f"MicroVM entered {state} before RUNNING")
        time.sleep(2)
    raise TimeoutError(f"MicroVM {microvm_id} did not reach RUNNING in {timeout}s")


def dispatch_job(endpoint: str, token: str, job: dict) -> None:
    """Fire-and-forget POST of the job to the runner. Returns 202."""
    req = urllib.request.Request(
        f"https://{endpoint}/run",
        method="POST",
        headers={
            "X-aws-proxy-auth": token,
            "X-aws-proxy-port": "9000",
            "Content-Type": "application/json",
        },
        data=json.dumps(job).encode(),
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status != 202:
            raise RuntimeError(f"runner did not accept the job: {r.status}")


def lambda_handler(event: dict, context) -> dict:
    record = event["Records"][0]

    # CodeCommit's TestRepositoryTriggers API (used by wire-codecommit.sh to
    # validate the wiring) sends a synthetic event with eventName
    # "TriggerEventTest". It carries the real branch ref, so without this
    # guard the orchestrator would launch a runner just to test the trigger.
    # Only react to real reference changes.
    if record.get("eventName") == "TriggerEventTest":
        logger.info("trigger test event; not launching a runner")
        return {"statusCode": 200, "body": "Trigger test event; no runner launched"}

    repo_name = record["eventSourceARN"].split(":")[-1]
    ref = record["codecommit"]["references"][0]["ref"]
    branch = ref.removeprefix("refs/heads/")
    source_commit = record["codecommit"]["references"][0]["commit"]

    pr = find_open_pr_for_branch(repo_name, branch)
    if not pr:
        # Module 3 uses its own branch (feature/ci-pipeline), which has no
        # pre-seeded PR, so open one on the first push and report status on it.
        pr = open_pull_request(repo_name, branch)
    if not pr:
        logger.info(f"no PR for {repo_name}#{branch} and none could be opened, skipping")
        return {"statusCode": 200, "body": f"No PR for {repo_name}#{branch}, skipping"}

    logger.info(f"launching runner for {repo_name}#{branch}")
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
            "maxIdleDurationSeconds": 60,
            "suspendedDurationSeconds": 60,
            "autoResumeEnabled": False,
        },
    )
    microvm_id = resp["microvmId"]
    endpoint = resp["endpoint"]

    wait_until_running(microvm_id)
    logger.info(f"microvm {microvm_id} running")

    token = microvms.create_microvm_auth_token(
        microvmIdentifier=microvm_id,
        expirationInMinutes=60,
        allowedPorts=[{"port": 9000}],
    )["authToken"]["X-aws-proxy-auth"]

    dispatch_job(endpoint, token, {
        "repo_name": repo_name,
        "branch": branch,
        "pr_id": pr["pr_id"],
        "source_commit": source_commit,
        "destination_commit": pr["destination_commit"],
        "region": REGION,
    })
    logger.info(f"dispatched job to runner, pr_id={pr['pr_id']}")

    return {"statusCode": 202, "body": json.dumps({"microvm_id": microvm_id, "pr_id": pr["pr_id"]})}
