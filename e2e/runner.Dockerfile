# The e2e test runner. It joins the `kind` Docker network, so slot IPs, the
# ingress and the cluster API are reachable the same way on Linux CI and on a
# Mac (where the kind network lives inside a VM).
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble
ARG KUBECTL_VERSION=v1.35.0
# HTTPS mirrors: plain-HTTP apt through some proxies yields 'Hash Sum mismatch'.
RUN sed -i 's|http://\(archive\|security\|ports\).ubuntu.com|https://\1.ubuntu.com|g' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends adb docker.io curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/$(dpkg --print-architecture)/kubectl" \
    && chmod +x /usr/local/bin/kubectl
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /work
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --group e2e --no-install-project
# The TypeScript MCP SDK, for the cross-client compatibility test.
COPY e2e/mcp-ts/package.json e2e/mcp-ts/package-lock.json /opt/mcp-ts/
RUN cd /opt/mcp-ts && npm ci --no-audit --no-fund
