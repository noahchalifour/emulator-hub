#!/usr/bin/env bash
# Runs the end-to-end suite inside the runner container on the `kind` Docker
# network (slot IPs and the ingress are reachable from there on Linux and macOS
# alike). Args go to pytest (default: tests/e2e), e.g.
#   ./e2e/run.sh tests/e2e -k lifecycle -x
#   ./e2e/run.sh tests/e2e/test_queue.py
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
STATE=$ROOT/e2e/.state
[[ -f $STATE/env ]] || { echo "run e2e/up.sh first" >&2; exit 2; }
source "$STATE/env"
docker build -q -t emulator-hub-e2e-runner -f e2e/runner.Dockerfile . >/dev/null
kind get kubeconfig --internal --name "$E2E_CLUSTER" >"$STATE/kubeconfig"
TTY=$([[ -t 1 ]] && echo -t || true)
exec docker run --rm -i $TTY --network kind \
  --add-host "hub.e2e.test:$E2E_INGRESS_IP" --add-host "mcp.e2e.test:$E2E_INGRESS_IP" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$ROOT:/work" -w /work \
  -e KUBECONFIG=/work/e2e/.state/kubeconfig -e E2E_STATE=/work/e2e/.state \
  -e E2E_ARTIFACTS_ALWAYS="${E2E_ARTIFACTS_ALWAYS:-}" \
  emulator-hub-e2e-runner \
  uv run --frozen --group e2e pytest -m e2e -p no:cacheprovider \
    -o addopts= --timeout=1800 -rA "${@:-tests/e2e}"
