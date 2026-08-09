# Base image for the "python" sandbox profile (ai_os/sandbox/container_runner.py).
# Includes pytest, pytest-cov, and git binaries ahead of time so offline validation works.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest pytest-cov
