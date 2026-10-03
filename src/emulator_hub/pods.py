"""The only module that talks to Kubernetes. Everything above it depends on
the `PodBackend` protocol, so tests use FakePods instead of a cluster."""

from typing import Protocol

from emulator_hub.catalog import DEVICE_MIN_RAM_MB, DISPLAY_OVERHEAD_MB, SYSTEM_IMAGES
from emulator_hub.models import Profile

LABEL_APP = "app.kubernetes.io/name"
LABEL_SLOT = "emulator-hub/slot"
LABEL_LEASE = "emulator-hub/lease"
APP = "emulator"
KVM = "squat.ai/kvm"
ADB_PORT = 5557  # socat bridge in the emulator image; the emulator's adbd binds loopback only
GRPC_PORT = 8554
KVM_GID = 993


def pod_name(slot: int, lease_id: str) -> str:
    return f"emu-slot-{slot}-{lease_id[:8]}"


def memory_mb(profile: Profile) -> tuple[int, int]:
    """(request, limit) in MiB. The guest gets at least the image's minimum RAM
    and the display adds host-side buffers; a limit below that is an OOMKill
    mid-boot, which the hub can only report as a failed boot."""
    guest = max(
        profile.ram_mb,
        SYSTEM_IMAGES[profile.system_image].min_ram_mb,
        DEVICE_MIN_RAM_MB.get(profile.device, 0),
    )
    overhead = DISPLAY_OVERHEAD_MB[profile.device]
    return guest + overhead, guest + overhead + 1024


def build_pod(*, namespace: str, image: str, slot: int, lease_id: str, profile: Profile) -> dict:
    sysimg = SYSTEM_IMAGES[profile.system_image]
    mem_request, mem_limit = memory_mb(profile)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name(slot, lease_id),
            "namespace": namespace,
            # velero.io/exclude-from-backup: the nightly Velero schedule runs
            # defaultVolumesToFsBackup across every namespace, so without this
            # an active lease at 02:00 uploads its 12Gi emptyDir AVD (the
            # Prometheus-TSDB mistake, docs/backups.md). Restoring a bare
            # emulator Pod would be wrong anyway: reconcile() deletes it.
            "labels": {
                LABEL_APP: APP,
                LABEL_SLOT: str(slot),
                LABEL_LEASE: lease_id,
                "velero.io/exclude-from-backup": "true",
            },
            # Bare Pods are never recreated; keep the descheduler's hands off
            # a device an agent is mid-test on.
            "annotations": {"descheduler.alpha.kubernetes.io/prefer-no-eviction": "true"},
        },
        "spec": {
            "restartPolicy": "Never",
            "priorityClassName": "emulator-hub-emulator",
            "enableServiceLinks": False,
            # /dev/kvm is 0660 root:kvm on the pve4 workers, kvm = gid 993
            # (verified 2026-09-24). The device plugin passes the node's
            # permissions through, so the non-root emulator needs the group.
            "securityContext": {"fsGroup": 10010, "supplementalGroups": [KVM_GID]},
            "automountServiceAccountToken": False,
            "nodeSelector": {"emulator-hub/kvm": "true"},
            "terminationGracePeriodSeconds": 5,
            "containers": [
                {
                    "name": "emulator",
                    "image": image,
                    "env": [
                        {"name": "SYSTEM_IMAGE", "value": sysimg.package},
                        {"name": "DEVICE", "value": profile.device},
                        {"name": "RAM_MB", "value": str(profile.ram_mb)},
                        {"name": "CORES", "value": str(profile.cores)},
                    ],
                    "ports": [
                        {"name": "adb", "containerPort": ADB_PORT},
                        {"name": "grpc", "containerPort": GRPC_PORT},
                    ],
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10010,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "resources": {
                        "requests": {
                            "cpu": str(profile.cores),
                            "memory": f"{mem_request}Mi",
                            "squat.ai/kvm": "1",
                        },
                        "limits": {"memory": f"{mem_limit}Mi", "squat.ai/kvm": "1"},
                    },
                    "volumeMounts": [{"name": "avd", "mountPath": "/avd"}],
                }
            ],
            "volumes": [{"name": "avd", "emptyDir": {"sizeLimit": "12Gi"}}],
        },
    }


class PodBackend(Protocol):
    async def create(self, manifest: dict) -> None: ...
    async def delete(self, name: str) -> None: ...
    async def list_emulators(self) -> dict[str, str]:
        """Pod name -> lease id, for every emulator Pod in the namespace."""
        ...

    async def pod_ip(self, name: str) -> str | None: ...
    async def is_failed(self, name: str) -> bool: ...


class KubePods:
    def __init__(self, namespace: str):
        from lightkube import AsyncClient

        self._ns = namespace
        self._client = AsyncClient(namespace=namespace)

    async def create(self, manifest: dict) -> None:
        from lightkube.resources.core_v1 import Pod

        await self._client.create(Pod.from_dict(manifest))

    async def delete(self, name: str) -> None:
        from lightkube.core.exceptions import ApiError
        from lightkube.resources.core_v1 import Pod

        try:
            await self._client.delete(Pod, name, grace_period=5)
        except ApiError as e:
            if e.status.code != 404:
                raise

    async def list_emulators(self) -> dict[str, str]:
        from lightkube.resources.core_v1 import Pod

        pods = {}
        async for pod in self._client.list(Pod, labels={LABEL_APP: APP}):
            pods[pod.metadata.name] = pod.metadata.labels.get(LABEL_LEASE, "")
        return pods

    async def _get(self, name: str):
        from lightkube.core.exceptions import ApiError
        from lightkube.resources.core_v1 import Pod

        try:
            return await self._client.get(Pod, name)
        except ApiError as e:
            if e.status.code == 404:
                return None
            raise

    async def pod_ip(self, name: str) -> str | None:
        pod = await self._get(name)
        return pod.status.podIP if pod and pod.status else None

    async def is_failed(self, name: str) -> bool:
        pod = await self._get(name)
        return pod is None or (pod.status is not None and pod.status.phase in ("Failed", "Succeeded"))
