# Kiro callback-enabled reviewer image for the durable-orchestration lab.
#
# Installs the Kiro CLI (headless) alongside git, the AWS CLI, and boto3.
# app.py implements the durable-execution callback pattern: POST /review
# returns 202 immediately, runs kiro-cli in a background thread, and signals
# completion via SendDurableExecutionCallbackSuccess. The Kiro API key is
# fetched from Secrets Manager at runtime (never baked into the image).

FROM python:3.12-slim

# System dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 (aarch64 — Lambda MicroVMs run on ARM64)
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o awscliv2.zip \
    && unzip -q awscliv2.zip \
    && ./aws/install \
    && rm -rf awscliv2.zip aws/

# Kiro CLI (headless)
RUN curl -fsSL https://cli.kiro.dev/install | bash
ENV PATH="/root/.local/bin:${PATH}"

# boto3 >= 1.43.0 for the SendDurableExecutionCallback* APIs
RUN pip install --no-cache-dir 'boto3>=1.43.0'

WORKDIR /workspace

COPY app.py .

# Single port for both the /review endpoint and lifecycle hooks
EXPOSE 9000

CMD ["python3", "app.py"]
