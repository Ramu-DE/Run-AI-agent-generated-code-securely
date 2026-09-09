# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.

# Use Python 3.12 as the base runtime
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .

# Expose the application port
# Port 443 is supported by default for inbound traffic (routed to 8080 inside the VM)
EXPOSE 8080

# Start the application
# Lambda will call the /ready hook on port 9000 to confirm startup
CMD ["python3", "app.py"]