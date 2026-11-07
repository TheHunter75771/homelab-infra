# gpu-broker image. The base is a build arg so the role can pass the digest-pinned tag from
# versions.yml (images.*); the default is only for local builds.
ARG PYTHON_IMAGE=python:3.14-slim

FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY requirements.lock /tmp/requirements.lock
# Wheels only (no compilers anywhere), every file hash-checked, and no pip inside the venv.
RUN python -m venv --without-pip /opt/venv \
 && pip --python /opt/venv/bin/python install --require-hashes --only-binary=:all: -r /tmp/requirements.lock

FROM ${PYTHON_IMAGE}
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    GPU_BROKER_CONFIG=/etc/gpu-broker/config.json
RUN groupadd --system --gid 10001 broker \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin broker
COPY --from=build /opt/venv /opt/venv
COPY gpu_broker /app/gpu_broker
WORKDIR /app
USER 10001:10001
# The default listen_port; the published port is whatever the config says.
EXPOSE 11434
# Reads the same config (GPU_BROKER_CONFIG) as the server, so it follows listen_host/listen_port.
# Point the config at a different path with the env var, not with --config, or the check won't find it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-m", "gpu_broker", "--healthcheck"]
ENTRYPOINT ["python", "-m", "gpu_broker"]
