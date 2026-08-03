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
# Known limitation (not fixed by this image): a project's OWN third-party
# dependencies (anything in its requirements.txt beyond pytest) still can't
# be installed inside a --network-none run for the same reason. Projects
# whose tests only need the standard library + pytest work today; genuine
# per-project dependency installation would need a separate "build an image
# for this task, then validate network-free" two-phase flow, which isn't
# built yet - flagged in CLAUDE.md as a known gap, not silently ignored.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest
