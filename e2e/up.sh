#!/usr/bin/env bash
# Brings up the end-to-end cluster: kind (3 nodes) + /dev/kvm device plugin +
# MetalLB slot IPs + ingress-nginx + fake authentik + NetworkPolicies + the hub
# built from this checkout. Idempotent: re-running redeploys the hub.
#
#   E2E_EMULATOR=real|fake   real: emulator/Dockerfile under KVM (Linux/x86_64
#                            with /dev/kvm). fake: e2e/fake-emulator, a gRPC
#                            stand-in that runs anywhere (default off-Linux).
#   E2E_SLOTS                number of slots (default: 2 real, 3 fake)
#   E2E_KVM_CAPACITY         squat.ai/kvm per node (default: E2E_SLOTS)
#
# Writes e2e/.state/env, which the test suite reads.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
STATE=$ROOT/e2e/.state
mkdir -p "$STATE/data/hub"
chmod 0777 "$STATE/data" "$STATE/data/hub"

CLUSTER=emulator-hub-e2e
if [[ -z "${E2E_EMULATOR:-}" ]]; then
  if [[ "$(uname -s)" == Linux && -e /dev/kvm ]]; then E2E_EMULATOR=real; else E2E_EMULATOR=fake; fi
fi
SLOTS=${E2E_SLOTS:-$([[ $E2E_EMULATOR == real ]] && echo 2 || echo 3)}
KVM_CAPACITY=${E2E_KVM_CAPACITY:-$SLOTS}
TAG=e2e
HUB_IMAGE=emulator-hub:$TAG
FAKE_IMAGE=emulator-hub-fake-emulator
TOOLBOX_IMAGE=emulator-hub-e2e-toolbox:$TAG
if [[ $E2E_EMULATOR == real ]]; then EMULATOR_IMAGE=emulator-hub-emulator:$TAG; else EMULATOR_IMAGE=$FAKE_IMAGE:ok; fi
BOOT_TIMEOUT_S=${E2E_BOOT_TIMEOUT_S:-$([[ $E2E_EMULATOR == real ]] && echo 480 || echo 60)}
API_TOKEN=${E2E_API_TOKEN:-e2e-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')}
[[ -f $STATE/env ]] && API_TOKEN=$(sed -n 's/^E2E_API_TOKEN=//p' "$STATE/env")

log() { printf '\n== %s\n' "$*"; }
render() { DATA_DIR=$STATE/data envsubst; }

log "images"
docker build -q -t "$HUB_IMAGE" . >/dev/null
for variant in ok:4 slow:75 never-boots:4 exit-during-boot:4; do
  docker build -q -t "$FAKE_IMAGE:${variant%%:*}" -f e2e/fake-emulator/Dockerfile \
    --build-arg FAKE_MODE="${variant%%:*}" --build-arg BOOT_DELAY_S="${variant##*:}" . >/dev/null
done
docker build -q -t "$TOOLBOX_IMAGE" -f e2e/toolbox.Dockerfile e2e >/dev/null
if [[ $E2E_EMULATOR == real ]] && ! docker image inspect "$EMULATOR_IMAGE" >/dev/null 2>&1; then
  docker build -t "$EMULATOR_IMAGE" emulator/
fi

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  log "kind cluster"
  export DATA_DIR=$STATE/data
  # Real KVM: bind the host's device into the node (a mknod'd copy inside the
  # node is not enough: vCPUs hang). Fake: nothing to mount.
  if [[ $E2E_EMULATOR == real ]]; then
    export KVM_MOUNT="      - {hostPath: /dev/kvm, containerPath: /dev/kvm}"
  else
    export KVM_MOUNT=""
  fi
  envsubst <e2e/kind.yaml >"$STATE/kind.yaml"
  kind create cluster --config "$STATE/kind.yaml" --wait 180s
fi
CTX=kind-$CLUSTER
K="kubectl --context $CTX"
KVM_NODE=$CLUSTER-worker2
HUB_NODE=$CLUSTER-worker

log "/dev/kvm on $KVM_NODE (0660 root:993, like the prod workers)"
# Real: the host's /dev/kvm, bind-mounted into the node by kind.yaml (a
# mknod'd copy is not enough: vCPUs hang). Fake: a stand-in char device
# (/dev/null's numbers) with prod's 0660 root:993, so the device plugin and the
# Pod's supplementalGroups are what grant access.
if [[ $E2E_EMULATOR == real ]]; then
  test -c /dev/kvm || { echo "E2E_EMULATOR=real needs /dev/kvm on the host" >&2; exit 2; }
  docker exec "$KVM_NODE" test -c /dev/kvm || { echo "kind node lacks the /dev/kvm mount: e2e/down.sh and retry" >&2; exit 2; }
  # Pods get /dev/kvm through the device plugin and reach it only via the
  # prod kvm gid (993). The bind mount shares the host's inode, so this
  # regroups the HOST device: fine on a throwaway CI runner, opt-in elsewhere.
  if [[ -n ${CI:-} || -n ${E2E_ALLOW_KVM_REGROUP:-} ]]; then
    docker exec "$KVM_NODE" sh -c "chgrp 993 /dev/kvm && chmod 0660 /dev/kvm"
  elif [[ "$(docker exec "$KVM_NODE" stat -c %g /dev/kvm)" != 993 ]]; then
    echo "host /dev/kvm is not group 993; set E2E_ALLOW_KVM_REGROUP=1 to let up.sh chgrp it" >&2
    exit 2
  fi
else
  docker exec "$KVM_NODE" sh -c "test -e /dev/kvm || mknod /dev/kvm c 1 3"
  docker exec "$KVM_NODE" sh -c "chown 0:993 /dev/kvm && chmod 0660 /dev/kvm"
fi

log "load images"
load() {  # load <image> [node...]: skip nodes that already have this exact image ID
  local img=$1 id node
  shift
  id=$(docker image inspect -f '{{.Id}}' "$img")
  for node in "$@"; do
    if [[ "$(docker exec "$node" crictl inspecti -o go-template --template '{{.status.id}}' "docker.io/library/$img" 2>/dev/null)" != "$id" ]]; then
      kind load docker-image --name "$CLUSTER" --nodes "$node" "$img"
    fi
  done
}
ALL_NODES=("$CLUSTER-control-plane" "$HUB_NODE" "$KVM_NODE")
for img in "$HUB_IMAGE" "$TOOLBOX_IMAGE"; do load "$img" "${ALL_NODES[@]}"; done
for v in ok slow never-boots exit-during-boot; do load "$FAKE_IMAGE:$v" "$KVM_NODE"; done
# The real emulator image is ~17 GB: only the KVM node ever runs it.
[[ $E2E_EMULATOR == real ]] && load "$EMULATOR_IMAGE" "$KVM_NODE"

log "metallb"
$K apply -f https://raw.githubusercontent.com/metallb/metallb/v0.16.1/config/manifests/metallb-native.yaml >/dev/null
$K -n metallb-system rollout status deploy/controller --timeout=180s
$K -n metallb-system wait --for=condition=Ready pod -l component=speaker --timeout=180s
SUBNET=$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' | tr ' ' '\n' | grep -m1 '\.')
PREFIX=$(echo "$SUBNET" | cut -d. -f1-2)
export POOL_START=$PREFIX.255.200 POOL_END=$PREFIX.255.250
until render <e2e/manifests/metallb-pool.yaml | $K apply -f - >/dev/null 2>&1; do sleep 2; done  # webhook warm-up

log "ingress-nginx"
$K apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/deploy/static/provider/cloud/deploy.yaml >/dev/null
$K -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=240s
$K -n ingress-nginx patch svc ingress-nginx-controller --type merge \
  -p "{\"metadata\":{\"annotations\":{\"metallb.io/loadBalancerIPs\":\"$PREFIX.255.200\"}}}" >/dev/null

log "kvm device plugin (capacity $KVM_CAPACITY)"
export KVM_CAPACITY
render <e2e/manifests/device-plugin.yaml | $K apply -f - >/dev/null
$K -n kube-system rollout status ds/generic-device-plugin --timeout=180s
until [[ "$($K get node "$KVM_NODE" -o jsonpath='{.status.allocatable.squat\.ai/kvm}')" == "$KVM_CAPACITY" ]]; do sleep 2; done

log "fake authentik + toolbox"
render <e2e/manifests/fake-authentik.yaml | $K apply -f - >/dev/null
export TOOLBOX_IMAGE
render <e2e/manifests/toolbox.yaml | $K apply -f - >/dev/null

log "hub ($SLOTS slots, $E2E_EMULATOR emulator)"
SLOT_IPS=""
: >"$STATE/slots.yaml"
for ((n = 0; n < SLOTS; n++)); do
  IP=$PREFIX.255.$((210 + n))
  SLOT_IPS+="${SLOT_IPS:+,}$IP"
  N=$n IP=$IP envsubst <e2e/manifests/slots.yaml.tmpl >>"$STATE/slots.yaml"
done
export HUB_IMAGE EMULATOR_IMAGE SLOT_IPS API_TOKEN BOOT_TIMEOUT_S
render <e2e/manifests/hub.yaml | $K apply -f - >/dev/null
$K apply -f "$STATE/slots.yaml" -f e2e/manifests/ingress.yaml -f e2e/manifests/netpol.yaml >/dev/null
$K -n emulator-hub rollout restart deploy/emulator-hub >/dev/null
$K -n emulator-hub rollout status deploy/emulator-hub --timeout=180s
$K -n e2e-auth rollout status deploy/fake-authentik --timeout=120s
$K -n e2e-client wait --for=condition=Ready pod/toolbox --timeout=120s
$K -n e2e-scratch wait --for=condition=Ready pod/toolbox --timeout=120s

cat >"$STATE/env" <<ENV
E2E_CONTEXT=$CTX
E2E_CLUSTER=$CLUSTER
E2E_EMULATOR=$E2E_EMULATOR
E2E_NAMESPACE=emulator-hub
E2E_INGRESS_IP=$PREFIX.255.200
E2E_SLOT_IPS=$SLOT_IPS
E2E_API_TOKEN=$API_TOKEN
E2E_HUB_IMAGE=$HUB_IMAGE
E2E_EMULATOR_IMAGE=$EMULATOR_IMAGE
E2E_FAKE_IMAGE=$FAKE_IMAGE
E2E_BOOT_TIMEOUT_S=$BOOT_TIMEOUT_S
E2E_KVM_NODE=$KVM_NODE
E2E_HUB_NODE=$HUB_NODE
E2E_KVM_CAPACITY=$KVM_CAPACITY
ENV
log "ready: $STATE/env"
cat "$STATE/env" | grep -v TOKEN
