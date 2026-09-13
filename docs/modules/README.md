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

## Measured cost of each module

From [../METRICS.md](../METRICS.md) — real runs, not estimates.

| Module | Image build | End to end | Billed compute | Note |
|---|---|---|---|---|
| 1 | 99–160 s | n/a (manual) | n/a | launch ~3.6 s, then ~35 ms per call |
| 2 | 153 s | **80.9 s** | **~3 s** across 4 invocations | AI call dominates (~60 s) |
| 3 | 120 s | **26.0 s** | ~1.7 s avg × 2 | ~25 s of the 26 is fixed overhead |
| 4 | 111 s | 2.73 s cold / ~60 ms warm | ~0.6 s avg × 16 | ≈45× cold-to-warm ratio |

Two conclusions worth carrying into your own designs:

- **Module 2 justifies durable execution; module 3 does not.** 80.9 s of wall-clock for
  ~3 s of billed compute is the whole point of suspending. Module 3 finishes in 26 s and
  never waits, so durability would be pure complexity.
- **There is a floor of roughly 12–15 s of fixed overhead** per triggered pipeline
  (trigger + launch + clone + comment). One VM per push is right for jobs measured in
  tens of seconds; for a 200 ms lint check the overhead dominates.

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
- **[../METRICS.md](../METRICS.md)** — measured build, launch, latency and pipeline numbers
- **[../TROUBLESHOOTING.md](../TROUBLESHOOTING.md)** — real root causes, including the false leads
