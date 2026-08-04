# Base image for the "python" sandbox profile (ai_os/sandbox/container_runner.py).
#
# Why this exists: the sandbox runs every validation with --network none (doc
# 10's own hardening requirement), so a plain `pip install ...` *inside* the
# validation container can never reach PyPI - there is no network to reach it
# with. Vanilla python:3.12-slim doesn't ship pytest, so without this image
# the python profile would always fail with "pytest: command not found",
# regardless of the project being validated. This image bakes pytest in
# ahead of time (built here, WITH network access, once) so the actual
# validation run stays fully network-isolated.
#
# Build once:
#   docker build -t ai-os-sandbox-python:3.12 -f docker/python-sandbox.Dockerfile .
#
# A project's OWN third-party dependencies (its requirements.txt) ARE now
# handled: `container_runner.py`'s two-phase flow builds a per-task image FROM
# this base that pip-installs requirements.txt WITH network (phase 1), then runs
# the tests against it with --network none (phase 2). So this base only needs
# pytest baked in; the rest comes from the project's manifest at task time.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest
