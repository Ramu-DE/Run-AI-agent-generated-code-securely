# Docker basics — and how this project uses them

You need to *write* Dockerfiles here, but you never *run* Docker. Understanding the
split matters, because it is the first thing that confuses people in this workshop.

## The three concepts

| Term | What it is | Analogy |
|---|---|---|
| **Dockerfile** | text recipe for building an image | the recipe |
| **Image** | immutable filesystem + metadata produced by the build | the sealed meal kit |
| **Container** | a running process started from an image | the meal being eaten |

An image is built **once** and run **many** times. Nothing you write to a container's
filesystem survives its death unless it goes to a volume or external store.

## Layers

Every instruction adds a layer, and layers are cached. Change one, and every layer
after it rebuilds. That single fact drives how you order a Dockerfile.

From `module-1/Dockerfile`:

```dockerfile
FROM python:3.12-slim        # base layer

WORKDIR /app

COPY requirements.txt .      # dependencies FIRST...
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .                # ...code LAST

EXPOSE 8080
CMD ["python3", "app.py"]
```

`requirements.txt` is copied before `app.py` deliberately. Editing `app.py` — which
happens constantly — invalidates only the final layer, so `pip install` stays cached.
Reverse the two lines and every code edit reinstalls all dependencies.

### Instructions used in this repo

| Instruction | Meaning |
|---|---|
| `FROM` | base image to build on |
| `WORKDIR` | default directory for later instructions and at runtime |
| `COPY` | copy files from the build context into the image |
| `RUN` | execute a command **at build time**, persisting the result as a layer |
| `ENV` | set an environment variable baked into the image |
| `EXPOSE` | document a port (documentation only — it opens nothing) |
| `CMD` | the process to start **at run time** |

`RUN` versus `CMD` is the classic mix-up: `RUN` happens while building, `CMD` happens
when starting. Exactly one `CMD` takes effect — the last one.

### `&&` chaining and cleanup

From `module-3-ci-runner/runner/Dockerfile`:

```dockerfile
RUN apt-get update && apt-get install -y \
    git curl unzip bash \
    && rm -rf /var/lib/apt/lists/*
```

One `RUN` instead of four means one layer. The `rm -rf` must be in the *same* `RUN`:
deleting files in a later layer hides them but the bytes still ship, because layers
are additive. Same reasoning behind `pip install --no-cache-dir`.

### Architecture matters here

```dockerfile
# AWS CLI v2 (aarch64 — Lambda MicroVMs run on ARM64)
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o awscliv2.zip \
    && unzip -q awscliv2.zip && ./aws/install && rm -rf awscliv2.zip aws/
```

MicroVMs are ARM64, confirmed by `platform.machine()` returning `aarch64` inside a
running VM. Downloading the x86_64 binary produces an image that builds cleanly and
then fails at runtime with `exec format error`.

### Baking configuration, not secrets

```dockerfile
ENV CLAUDE_CODE_USE_BEDROCK=1
ENV ANTHROPIC_MODEL=us.anthropic.claude-sonnet-4-6
```

Non-secret configuration is fine in `ENV`. Credentials are not: anyone who can pull
the image can read them, including through image history. In this project the
reviewer authenticates to Bedrock purely through the MicroVM's **IAM execution role**
— no API key exists in the image at all. That is the pattern to copy.

`module-2/reviewer-claude/Dockerfile` also deliberately does *not* pin `AWS_REGION`,
because the app sets it per request from the incoming payload. Baking a region into
an image makes it single-region.

## The twist: AWS builds these images, not you

Normally you would run `docker build` locally and push to a registry. Here the
`build-image.sh` scripts do this instead:

```bash
zip -qr app.zip app.py Dockerfile                       # 1. package the build context
aws s3 cp app.zip "s3://$ARTIFACTS_BUCKET/$S3_KEY"      # 2. upload it

aws lambda-microvms create-microvm-image \               # 3. AWS builds it
  --code-artifact "uri=s3://$ARTIFACTS_BUCKET/$S3_KEY" \
  --base-image-arn "arn:aws:lambda:$AWS_REGION:aws:microvm-image:al2023-1" \
  --build-role-arn "$MODULE2_REVIEWER_BUILD_ROLE_ARN" \
  ...
```

The zip **is** the Docker build context. AWS assumes the build role, runs the build
server-side, and converts the result into a bootable MicroVM image with a snapshot.

Consequences worth internalising:

- **No local Docker daemon is required.** `docker` is not installed in this
  environment and all four modules still build. If you assumed Docker was the
  blocker, that is a false lead.
- The build log lands in **CloudWatch**, not your terminal. Real build output from
  `/aws/lambda-microvms/mvm-code-execution-sandbox`:
  ```
  #8 [4/5] RUN pip install --no-cache-dir -r requirements.txt
  #9 [5/5] COPY app.py .
  #10 exporting to image ... DONE 0.3s
  ```
  That is BuildKit — the same builder as local Docker.
- Only what you `zip` exists. Forget to add a file and the build fails on `COPY`.
- The build role needs `s3:GetObject` on the artifacts bucket, or the build cannot
  read its own context.

## Container versus MicroVM

The Dockerfile defines the filesystem; it does **not** mean you get a container.
Lambda converts the image into a VM with its own kernel, then snapshots it. Two
practical differences:

1. **The snapshot is taken after your `ready` hook returns 200.** Your app must be
   fully started by then, because every future launch restores that exact memory
   state. See [MICROVM.md](MICROVM.md).
2. **Multiple processes and long-lived threads are fine.** Module 1 runs two HTTP
   servers on two ports in one image — normal for a VM, awkward for a container.

## Debugging without a local daemon

Since you cannot `docker run` the image, keep the app runnable as a plain script and
test the handlers directly over a socket. That is how the module-1 hook bug was
verified before spending ~2 minutes on a rebuild:

```bash
python3 app.py &                        # the CMD, run directly
curl -X POST localhost:9000/aws/lambda-microvms/runtime/v1/run -d '{}'
```

Keeping `CMD ["python3", "app.py"]` this simple is what makes that possible. The
loop is: test locally → publish a new image version → launch → read CloudWatch.
