# Module flows — index

One detailed walkthrough per module. Each explains the architecture, the complete flow,
and **the purpose of every command and flag**.

| Module | Walkthrough | Builds | New concept introduced |
|---|---|---|---|
| 1 | **[MODULE-1.md](MODULE-1.md)** | sandboxed code-execution API | MicroVM lifecycle, hooks, snapshots, auth tokens |
| 2 | **[MODULE-2.md](MODULE-2.md)** | AI reviewer on pull requests | durable execution + callback pattern |
| 3 | **[MODULE-3.md](MODULE-3.md)** | ephemeral CI runner | one disposable VM per push, pipeline-in-repo |
| 4 | **[MODULE-4.md](MODULE-4.md)** | multi-tenant SaaS control plane | VM per tenant + token vending machine |

Read them in order. Each reuses the previous module's machinery and adds one idea.

---

## The progression

```
Module 1   you drive every API call by hand
             zip -> S3 -> create-microvm-image -> run-microvm -> token -> curl
             |
             |  "now automate it"
             v
Module 2   a Lambda does it for you, triggered by a git push
             + durable execution, because AI review takes minutes
             |
             |  "drop the waiting; let the VM report for itself"
             v
Module 3   same trigger, no durability needed
             + one-shot VM per push, pipeline defined in the repo
             |
             |  "one VM per tenant instead of one per job"
             v
Module 4   always-on API in front of many long-lived VMs
             + IAM session policies for data isolation
```

---

## Same four steps every time (modules 2–4)

```
1  build-image.sh   zip -> S3 -> create-microvm-image -> poll -> persist <X>_IMAGE_ARN
2  deploy.sh        sam build -> sam deploy -> ':live' alias
3  wire-*.sh        lambda add-permission  +  codecommit put-repository-triggers
4  prepare-*.sh     stage files, then YOU commit and push
```

**Order is load-bearing.** Step 3 binds to the alias created in step 2. Run it against a
stack that was never deployed and you get a trigger pointing at nothing — pushes then do
nothing, with no error anywhere. That was the original failure in this repo; see
[../TROUBLESHOOTING.md](../TROUBLESHOOTING.md).

Module 1 has no scripts on purpose — you type the same calls yourself so the automation
in 2–4 is recognisable.

---

## Design choices compared

| | Module 1 | Module 2 | Module 3 | Module 4 |
|---|---|---|---|---|
| Trigger | you, manually | CodeCommit push | CodeCommit push | HTTPS request |
| Orchestrator | none | durable Lambda | plain Lambda | Lambda + HTTP API |
| VM lifetime | you decide | one review | one build | one tenant, long-lived |
| `autoResumeEnabled` | `true` | n/a | **`false`** (one-shot) | **`true`** (stay warm) |
| `maxIdleDurationSeconds` | 600 | 300 | **60** | **900** |
| Memory floor | 2048 MiB | 2048 MiB | 2048 MiB | **1024 MiB** |
| Waits for a result? | you do | **yes — suspends** | no | yes, inline |
| Who ends the VM? | idle policy | orchestrator terminates | idle policy | idle policy |
| Ports in the VM | 8080 app + 9000 hooks | 9000 (both) | 9000 (both) | 8080 app + 9000 hooks |

The idle-policy row is the interesting one: the same three fields express "one-shot build
machine" and "warm tenant server" with no other code changes.

---

## The hook rule, in one line

**A lifecycle hook that returns anything other than 200 destroys the MicroVM.**

Module 1 learned it the hard way — it answered `/launch` while the runtime posts `/run`,
so the VM died two seconds after every launch. Module 3 shows the pattern that makes the
mistake impossible:

```python
if self.path == "/run":          # exact match: MY job endpoint
    ...
self._json(200, {"status": "ok"})  # everything else, including all hooks
```

Details and evidence: [../MICROVM.md](../MICROVM.md) and
[../TROUBLESHOOTING.md](../TROUBLESHOOTING.md).

---

## Also worth reading

- **[../MICROVM.md](../MICROVM.md)** — images vs versions, hook contract, tokens, connectors, idle policy, IAM
- **[../DOCKER.md](../DOCKER.md)** — Docker basics, and why no local daemon is needed
- **[../CICD.md](../CICD.md)** — trigger wiring, durable execution, debugging a dead pipeline
- **[../COMMANDS.md](../COMMANDS.md)** — every command used, with its purpose
- **[../TROUBLESHOOTING.md](../TROUBLESHOOTING.md)** — real root causes, including the false leads
