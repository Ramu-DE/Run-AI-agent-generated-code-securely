# Per-tenant application image for the Module 4 SaaS lab.
#
# A small Python app (app.py) that serves tenant-scoped info on port 8080
# and answers the MicroVM 'ready' lifecycle hook on port 9000. Every tenant
# runs its own MicroVM launched from this image. The app uses boto3 to assume
# a tenant-scoped role with a dynamically generated session policy (the token
# vending machine pattern) so each tenant can only reach its own S3 prefix.

FROM python:3.12-slim

RUN pip install --no-cache-dir boto3

WORKDIR /workspace
COPY app.py .

EXPOSE 8080 9000
CMD ["python3", "app.py"]
