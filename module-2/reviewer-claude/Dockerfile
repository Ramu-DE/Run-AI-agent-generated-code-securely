# Module 2 callback-enabled reviewer image.
#
# Uses Claude Code CLI (headless -p mode) for AI code review inside a
# Lambda MicroVM. The app.py implements the durable-execution callback
# pattern: POST /review returns 202 immediately, runs Claude in a
# background thread, and signals completion via
# SendDurableExecutionCallbackSuccess.

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

# Node.js (required for Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI (headless)
RUN npm install -g @anthropic-ai/claude-code

# boto3 >= 1.43.0 for the SendDurableExecutionCallback* APIs
RUN pip install --no-cache-dir 'boto3>=1.43.0'

# Claude Code -> Amazon Bedrock. Credentials come from the MicroVM execution
# role at runtime (no API key is baked in). AWS_REGION is intentionally NOT
# pinned here because the workshop is multi-region; app.py sets it per request
# from the incoming review payload.
ENV CLAUDE_CODE_USE_BEDROCK=1
ENV ANTHROPIC_MODEL=us.anthropic.claude-sonnet-4-6

WORKDIR /workspace

COPY app.py .

# Single port for both the /review endpoint and lifecycle hooks
EXPOSE 9000

CMD ["python3", "app.py"]
