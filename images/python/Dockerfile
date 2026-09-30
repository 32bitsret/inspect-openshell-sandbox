# Sandbox image for OpenShell-backed Inspect tasks.
#
# OpenShell runs the sandbox as an unprivileged user (UID 1000) and, on the
# Docker driver, uses the image's WORKDIR as the workspace provided it exists
# and is writable by that user. So the workspace is created here, at build
# time, rather than at runtime where the sandbox user cannot create it.
FROM python:3.12-slim
RUN mkdir -p /space && chown 1000:1000 /space
WORKDIR /space
