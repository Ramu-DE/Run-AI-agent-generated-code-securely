# Ephemeral CI runner image for the Module 3 lab.
#
# A small HTTP agent (app.py) that clones a CodeCommit repo, runs the
# repo's ci/steps.sh pipeline, and posts the pass/fail result to the
# pull request. Runs inside a Lambda MicroVM.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    git \
    curl \
    unzip \
    bash \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 (aarch64 - Lambda MicroVMs run on ARM64)
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o awscliv2.zip \
    && unzip -q awscliv2.zip \
    && ./aws/install \
    && rm -rf awscliv2.zip aws/

RUN pip install --no-cache-dir 'boto3>=1.43.0'

WORKDIR /workspace
COPY app.py .

EXPOSE 9000
CMD ["python3", "app.py"]
