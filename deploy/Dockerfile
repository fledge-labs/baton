# SPDX-License-Identifier: Apache-2.0
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY deploy/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /app/requirements.txt && useradd -m -u 10001 agent
WORKDIR /app
COPY bridge /app/bridge
COPY scripts /app/scripts
COPY deploy/gateway.template.json /app/deploy/gateway.template.json
COPY deploy/migrate.py /app/deploy/migrate.py
RUN mkdir -p /data && chown -R agent:agent /data /home/agent
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/home/agent
CMD ["python", "-m", "bridge.server"]
