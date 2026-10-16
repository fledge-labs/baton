# SPDX-License-Identifier: Apache-2.0
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS codex
RUN npm install -g @openai/codex@0.154.0
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git bubblewrap && rm -rf /var/lib/apt/lists/*
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
COPY deploy/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && useradd -m -u 10001 agent
WORKDIR /app
COPY bridge /app/bridge
COPY scripts /app/scripts
COPY deploy/gateway.template.json /app/deploy/gateway.template.json
RUN mkdir -p /data /work /home/agent/.codex && chown -R agent:agent /data /work /home/agent
# The pinned CLI bundles a newer bubblewrap with argv0 support.
# Remove the distro helper so the CLI selects its own pinned implementation.
RUN apt-get purge -y bubblewrap
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/home/agent CODEX_HOME=/home/agent/.codex
CMD ["python", "-m", "bridge.server"]
