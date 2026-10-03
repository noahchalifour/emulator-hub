"""Clients and helpers the end-to-end tests drive the deployed hub with.

Everything here talks to the cluster the way a user does: MCP over HTTP through
the ingress, REST/WebSocket through the ingress + fake authentik, adb through a
slot's LoadBalancer IP. kubectl is used to observe and to inject faults only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("E2E_STATE", ROOT / "e2e" / ".state"))
APK = ROOT / "e2e" / "test-apk" / "e2e-probe.apk"
UI_HOST = "hub.e2e.test"
MCP_HOST = "mcp.e2e.test"
NS = "emulator-hub"


@dataclass(frozen=True)
class Env:
    context: str
    cluster: str
    emulator: str  # "real" | "fake"
    ingress_ip: str
    slot_ips: tuple[str, ...]
    api_token: str
    hub_image: str
    emulator_image: str
    fake_image: str
    boot_timeout_s: float
    kvm_node: str
    hub_node: str
    kvm_capacity: int

    @property
    def real(self) -> bool:
        return self.emulator == "real"

    @property
    def boot_budget_s(self) -> float:
        """How long a test waits for a boot it expects to succeed."""
        return self.boot_timeout_s + 60

    @classmethod
    def load(cls) -> Env:
        path = STATE / "env"
        if not path.exists():
            raise FileNotFoundError(f"{path} missing: run e2e/up.sh first")
        kv = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
        return cls(
            context=kv["E2E_CONTEXT"],
            cluster=kv["E2E_CLUSTER"],
            emulator=kv["E2E_EMULATOR"],
            ingress_ip=kv["E2E_INGRESS_IP"],
            slot_ips=tuple(kv["E2E_SLOT_IPS"].split(",")),
            api_token=kv["E2E_API_TOKEN"],
            hub_image=kv["E2E_HUB_IMAGE"],
            emulator_image=kv["E2E_EMULATOR_IMAGE"],
            fake_image=kv["E2E_FAKE_IMAGE"],
            boot_timeout_s=float(kv["E2E_BOOT_TIMEOUT_S"]),
            kvm_node=kv["E2E_KVM_NODE"],
            hub_node=kv["E2E_HUB_NODE"],
            kvm_capacity=int(kv["E2E_KVM_CAPACITY"]),
        )


# ---------------------------------------------------------------- polling


async def wait_until(
    pred: Callable[[], Awaitable[Any]] | Callable[[], Any], timeout: float, interval: float = 0.5, what: str = ""
):
    """Poll `pred` until it returns a truthy value and return that value."""
    deadline = time.monotonic() + timeout
    last_exc: BaseException | None = None
    while True:
        try:
            value = pred()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return value
        except AssertionError as exc:
            last_exc = exc
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what or pred}") from last_exc
        await asyncio.sleep(interval)


# ---------------------------------------------------------------- kubectl


class Kube:
    def __init__(self, env: Env):
        self.env = env

    def run(self, *args: str, check: bool = True, input: str | None = None, timeout: float = 120) -> str:
        cmd = ["kubectl", "--context", self.env.context, *args]
        res = subprocess.run(cmd, capture_output=True, text=True, input=input, timeout=timeout)
        if check and res.returncode != 0:
            raise RuntimeError(f"{shlex.join(cmd)} failed ({res.returncode}): {res.stderr.strip()} {res.stdout[-500:]}")
        return res.stdout

    def json(self, *args: str) -> Any:
        return json.loads(self.run(*args, "-o", "json"))

    def emulator_pods(self) -> list[dict]:
        return self.json("-n", NS, "get", "pods", "-l", "app.kubernetes.io/name=emulator")["items"]

    def pod(self, name: str) -> dict | None:
        out = self.run("-n", NS, "get", "pod", name, "-o", "json", "--ignore-not-found")
        return json.loads(out) if out.strip() else None

    def pod_names(self) -> set[str]:
        return {p["metadata"]["name"] for p in self.emulator_pods() if not p["metadata"].get("deletionTimestamp")}

    def logs(self, name: str, ns: str = NS, previous: bool = False) -> str:
        args = ["-n", ns, "logs", name] + (["--previous"] if previous else [])
        return self.run(*args, check=False)

    def exec(self, pod: str, *cmd: str, ns: str = NS, check: bool = True, timeout: float = 60) -> str:
        return self.run("-n", ns, "exec", pod, "--", *cmd, check=check, timeout=timeout)

    def hub_pod(self) -> dict:
        pods = [
            p
            for p in self.json("-n", NS, "get", "pods", "-l", "app.kubernetes.io/name=emulator-hub")["items"]
            if not p["metadata"].get("deletionTimestamp")
        ]
        assert len(pods) == 1, f"expected one hub Pod, found {[p['metadata']['name'] for p in pods]}"
        return pods[0]

    def hub_pods_all(self) -> list[dict]:
        return self.json("-n", NS, "get", "pods", "-l", "app.kubernetes.io/name=emulator-hub")["items"]

    def apply(self, manifest: dict | str) -> None:
        self.run("apply", "-f", "-", input=manifest if isinstance(manifest, str) else json.dumps(manifest))

    def delete_pod(self, name: str, grace: int | None = None, wait: bool = False) -> None:
        args = ["-n", NS, "delete", "pod", name, "--ignore-not-found", f"--wait={str(wait).lower()}"]
        if grace is not None:
            args.append(f"--grace-period={grace}")
            if grace == 0:
                args.append("--force")
        self.run(*args)

    def kill_container(self, pod_name: str, signal: str = "KILL") -> None:
        """Signal a Pod's main process from its node: PID 1 ignores SIGKILL
        sent from inside its own PID namespace."""
        pod = self.pod(pod_name)
        assert pod, f"no Pod {pod_name}"
        cid = pod["status"]["containerStatuses"][0]["containerID"].split("://", 1)[1]
        node = pod["spec"]["nodeName"]
        pid = self.node_exec(node, "crictl", "inspect", "-o", "go-template", "--template", "{{.info.pid}}", cid).strip()
        self.node_exec(node, "kill", f"-{signal}", pid)

    def node_exec(self, node: str, *cmd: str, check: bool = True) -> str:
        res = subprocess.run(["docker", "exec", node, *cmd], capture_output=True, text=True, timeout=120)
        if check and res.returncode != 0:
            raise RuntimeError(f"docker exec {node} {shlex.join(cmd)}: {res.stderr.strip()}")
        return res.stdout


# ---------------------------------------------------------------- hub control


class HubControl:
    """Restart, reconfigure and kill the hub Deployment; read its database."""

    def __init__(self, kube: Kube):
        self.kube = kube

    def env_of(self) -> dict[str, str]:
        dep = self.kube.json("-n", NS, "get", "deploy", "emulator-hub")
        return {
            e["name"]: e.get("value", "")
            for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
            if "value" in e
        }

    def set_env(self, **values: str | None) -> None:
        """Change env (None removes) and wait for the new Pod to be ready."""
        args = ["-n", NS, "set", "env", "deploy/emulator-hub"]
        args += [f"{k}={v}" if v is not None else f"{k}-" for k, v in values.items()]
        self.kube.run(*args)
        self.wait_rollout()

    def set_image(self, image: str) -> None:
        self.kube.run("-n", NS, "set", "image", "deploy/emulator-hub", f"hub={image}")
        self.wait_rollout()

    def restart(self) -> None:
        self.kube.run("-n", NS, "rollout", "restart", "deploy/emulator-hub")
        self.wait_rollout()

    def wait_rollout(self, timeout: int = 180) -> None:
        self.kube.run(
            "-n", NS, "rollout", "status", "deploy/emulator-hub", f"--timeout={timeout}s", timeout=timeout + 10
        )
        self.wait_serving()

    def wait_serving(self, timeout: float = 90) -> None:
        """Ready Pods precede ingress-nginx's endpoint update by a moment: wait
        until both ports answer through the ingress, consistently."""
        pod_uid = self.kube.hub_pod()["metadata"]["uid"]
        deadline = time.monotonic() + timeout
        ok = 0
        while time.monotonic() < deadline:
            try:
                ui = httpx.get(f"http://{UI_HOST}/api/me", cookies={"e2e_user": "e2e-probe"}, timeout=5)
                machine = httpx.post(
                    f"http://{MCP_HOST}/mcp",
                    json=jsonrpc("tools/list"),
                    headers={
                        "Authorization": f"Bearer {self.kube.env.api_token}",
                        "Accept": "application/json, text/event-stream",
                    },
                    timeout=5,
                )
                ok = ok + 1 if ui.status_code == 200 and machine.status_code == 200 else 0
            except httpx.HTTPError:
                ok = 0
            if ok >= 5:
                return
            time.sleep(0.5)
        raise AssertionError(f"hub {pod_uid} not serving through the ingress after {timeout}s")

    def kill(self) -> str:
        """Crash the hub: SIGKILL its process from the node (no SIGTERM, no
        shutdown hooks), then wait for the restarted container to serve."""
        pod = self.kube.hub_pod()
        name = pod["metadata"]["name"]
        restarts = pod["status"]["containerStatuses"][0]["restartCount"]
        self.kube.kill_container(name)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            p = self.kube.pod(name)
            st = (p or {}).get("status", {}).get("containerStatuses", [{}])[0]
            if st.get("restartCount", 0) > restarts and st.get("ready"):
                self.wait_serving()
                return name
            time.sleep(1)
        raise AssertionError("hub did not come back after kill")

    def sql(self, query: str, attempts: int = 3) -> list[list[Any]]:
        last: Exception | None = None
        for _ in range(attempts):
            try:
                return self._sql(query)
            except RuntimeError as exc:
                last = exc
                time.sleep(2)
        raise AssertionError(f"sql failed {attempts}x: {last}")

    def scale(self, replicas: int) -> None:
        self.kube.run("-n", NS, "scale", "deploy/emulator-hub", f"--replicas={replicas}")
        if replicas == 0:
            deadline = time.monotonic() + 120
            while self.kube.hub_pods_all() and time.monotonic() < deadline:
                time.sleep(1)
            assert not self.kube.hub_pods_all(), "hub Pod did not go away"
        else:
            self.wait_rollout()

    def logs(self) -> str:
        return self.kube.run("-n", NS, "logs", "deploy/emulator-hub", "--tail=-1", check=False)

    def proc(self, path: str) -> str:
        """Read a file from the hub container's /proc/1 (RSS, fds)."""
        name = self.kube.hub_pod()["metadata"]["name"]
        return self.kube.exec(name, "python", "-c", f"import os,sys;print(open('/proc/1/{path}').read())")

    def fd_count(self) -> int:
        name = self.kube.hub_pod()["metadata"]["name"]
        return int(self.kube.exec(name, "python", "-c", "import os;print(len(os.listdir('/proc/1/fd')))").strip())

    def rss_kb(self) -> int:
        for line in self.proc("status").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
        raise AssertionError("no VmRSS")

    def _sql(self, query: str) -> list[list[Any]]:
        """Run SQL against /data/hub.db from a throwaway Pod on the shared volume."""
        script = (
            "import json,sqlite3,sys;"
            "c=sqlite3.connect('/data/hub.db');"
            "r=[list(x) for x in c.execute(sys.argv[1])];c.commit();print(json.dumps(r))"
        )
        name = f"sql-{int(time.time() * 1000) % 10**9}"
        overrides = {
            "spec": {
                "securityContext": {"runAsUser": 10011, "fsGroup": 10011},
                "containers": [
                    {
                        "name": name,
                        "image": self.kube.env.hub_image,
                        "command": ["python", "-c", script, query],
                        "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                    }
                ],
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "emulator-hub-data"}}],
            }
        }
        out = self.kube.run(
            "-n",
            NS,
            "run",
            name,
            "--rm",
            "-i",
            "--quiet",
            "--restart=Never",
            f"--image={self.kube.env.hub_image}",
            "--image-pull-policy=IfNotPresent",
            f"--overrides={json.dumps(overrides)}",
            timeout=120,
        )
        return json.loads(out.strip().splitlines()[-1])


@contextlib.contextmanager
def port_forward(kube: Kube, *ports: int):
    """kubectl port-forward to the hub Service, bypassing the ingress (and, by
    design, NetworkPolicy). Yields {remote_port: local_port}."""
    import socket

    local = {}
    for p in ports:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            local[p] = s.getsockname()[1]
    cmd = ["kubectl", "--context", kube.env.context, "-n", NS, "port-forward", "svc/emulator-hub"]
    cmd += [f"{local[p]}:{p}" for p in ports]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", local[ports[0]]), timeout=1):
                    break
            except OSError:
                time.sleep(0.3)
        yield local
    finally:
        proc.terminate()
        proc.wait(10)


def metrics_text() -> str:
    return httpx.get(f"http://{MCP_HOST}/metrics", timeout=30).text


def metric(text: str, name: str, labels: str = "") -> float:
    """Value of one sample, e.g. metric(t, "emulator_hub_leases_ended_total", 'reason="lost"')."""
    key = f"{name}{{{labels}}}" if labels else name
    for line in text.splitlines():
        if line.startswith(key + " "):
            return float(line.split()[-1])
    return 0.0


# ---------------------------------------------------------------- MCP


class McpError(Exception):
    pass


class Mcp:
    """One connected MCP client (official Python SDK, streamable HTTP)."""

    def __init__(self, client: Client):
        self.client = client

    async def call(self, tool: str, **args) -> dict:
        result = await self.client.call_tool(tool, args, read_timeout_seconds=900)
        if result.is_error:
            raise McpError(" ".join(getattr(c, "text", "") for c in result.content))
        return result.structured_content

    async def call_raw(self, tool: str, **args):
        return await self.client.call_tool(tool, args, read_timeout_seconds=900)


@contextlib.asynccontextmanager
async def mcp_session(env: Env, token: str | None = None, timeout: float = 900):
    headers = {"Authorization": f"Bearer {token if token is not None else env.api_token}"}
    http = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout, connect=10))
    async with http, Client(streamable_http_client(f"http://{MCP_HOST}/mcp", http_client=http)) as c:
        yield Mcp(c)


class McpHolder:
    """Keeps an MCP session open in a task of its own, so a pytest fixture can
    set it up and tear it down from different tasks (anyio cancel scopes must
    be exited by the task that entered them)."""

    def __init__(self, env: Env, token: str | None = None):
        self.env, self.token = env, token
        self._stop = asyncio.Event()
        self._ready: asyncio.Future[Mcp] | None = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> Mcp:
        self._ready = asyncio.get_running_loop().create_future()

        async def hold():
            # Connecting right after a hub restart can hit the old backend for a
            # moment (ingress endpoint lag): retry the handshake, never a call.
            for attempt in range(10):
                try:
                    async with mcp_session(self.env, self.token) as m:
                        self._ready.set_result(m)
                        await self._stop.wait()
                    return
                except BaseException as exc:
                    if self._ready.done():
                        return  # the session was used; the caller saw any error
                    if attempt == 9 or isinstance(exc, asyncio.CancelledError):
                        self._ready.set_exception(exc)
                        return
                    await asyncio.sleep(1)

        self._task = asyncio.create_task(hold())
        return await self._ready

    async def __aexit__(self, *exc) -> None:
        self._stop.set()
        await asyncio.gather(self._task, return_exceptions=True)


def mcp_raw_client(env: Env, token: str | None = None, **kw) -> httpx.AsyncClient:
    """httpx pointed at the MCP ingress, for hand-built JSON-RPC requests."""
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = token
    return httpx.AsyncClient(base_url=f"http://{MCP_HOST}", headers=headers, timeout=kw.pop("timeout", 60), **kw)


def jsonrpc(method: str, params: dict | None = None, id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}


# ---------------------------------------------------------------- UI / REST


def ui_client(user: str | None = "noah", **kw) -> httpx.AsyncClient:
    """httpx through ingress-nginx; `user` is the fake-authentik session."""
    cookies = {"e2e_user": user} if user else {}
    return httpx.AsyncClient(base_url=f"http://{UI_HOST}", cookies=cookies, timeout=kw.pop("timeout", 60), **kw)


def ws_url(lease_id: str) -> str:
    return f"ws://{UI_HOST}/api/leases/{lease_id}/live"


# ---------------------------------------------------------------- lease helpers


async def acquire_leased(mcp: Mcp, env: Env, profile: str, holder: str = "e2e", ttl_minutes: int = 30) -> dict:
    """Acquire and poll heartbeat until the lease is `leased`."""
    grant = await mcp.call("acquire", profile=profile, holder=holder, ttl_minutes=ttl_minutes, boot_wait_seconds=0)
    return await wait_leased(mcp, env, grant)


async def wait_leased(mcp: Mcp, env: Env, grant: dict) -> dict:
    async def poll():
        nonlocal grant
        if grant["state"] == "leased":
            return grant
        grant = await mcp.call("heartbeat", lease_id=grant["lease_id"])
        return grant if grant["state"] == "leased" else None

    return await wait_until(poll, env.boot_budget_s, interval=2, what=f"lease {grant['lease_id']} to boot")


async def status(mcp: Mcp) -> dict:
    return await mcp.call("status")


def pod_name(slot: int, lease_id: str) -> str:
    return f"emu-slot-{slot}-{lease_id[:8]}"


# ---------------------------------------------------------------- adb


class Adb:
    """adb with a per-run key that the emulator has never seen."""

    def __init__(self, home: Path):
        self.home = home
        self.env = {**os.environ, "HOME": str(home), "ANDROID_USER_HOME": str(home / ".android")}
        self.env.pop("ADB_VENDOR_KEYS", None)
        (home / ".android").mkdir(parents=True, exist_ok=True)
        key = home / ".android" / "adbkey"
        if not key.exists():
            subprocess.run(["adb", "keygen", str(key)], env=self.env, capture_output=True, check=True)
        subprocess.run(["adb", "start-server"], env=self.env, capture_output=True)

    def run(self, *args: str, timeout: float = 120, check: bool = True) -> str:
        res = subprocess.run(["adb", *args], env=self.env, capture_output=True, text=True, timeout=timeout)
        if check and res.returncode != 0:
            raise RuntimeError(f"adb {shlex.join(args)}: {res.stdout} {res.stderr}")
        return res.stdout

    def connect(self, target: str, timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            last = self.run("connect", target, check=False, timeout=30)
            state = self.run("-s", target, "get-state", check=False, timeout=30).strip()
            if state == "device":
                return
            time.sleep(2)
        raise AssertionError(f"adb could not reach {target} as `device`: {last} / {self.devices()}")

    def devices(self) -> str:
        return self.run("devices", check=False)

    def shell(self, target: str, cmd: str, timeout: float = 120) -> str:
        return self.run("-s", target, "shell", cmd, timeout=timeout).replace("\r", "")

    def disconnect(self, target: str) -> None:
        self.run("disconnect", target, check=False)

    def install(self, target: str, apk, attempts: int = 5) -> None:
        """adb install, retried: right after boot the package manager can
        refuse with an empty reason."""
        last: Exception | None = None
        for _ in range(attempts):
            try:
                self.run("-s", target, "install", "-r", "-g", str(apk), timeout=300)
                return
            except RuntimeError as exc:
                last = exc
                time.sleep(5)
        raise AssertionError(f"install failed {attempts}x: {last}")

    def focus_app(self, target: str, component: str, timeout: float = 90) -> None:
        """Start `component` and keep dismissing the keyguard until it really
        holds input focus: a -wipe-data boot raises the keyguard a little after
        sys.boot_completed, and anything sent before then goes to the lock screen."""
        package = component.split("/")[0]
        self.shell(target, "settings put secure lockscreen.disabled 1; locksettings set-disabled true")
        deadline = time.monotonic() + timeout
        focus = ""
        while time.monotonic() < deadline:
            focus = self.shell(target, "dumpsys window | grep mCurrentFocus")
            if "Not Responding" in focus:
                # A launcher ANR dialog on a slow (nested-virt) boot: dismiss it ("Wait").
                self.shell(target, "input keyevent 4; am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS")
            self.shell(target, "wm dismiss-keyguard; input keyevent 82")
            self.shell(target, f"am start -W -n {component}")
            time.sleep(2)
            focus = self.shell(target, "dumpsys window | grep mCurrentFocus")
            keyguard = self.shell(target, "dumpsys window | grep isKeyguardShowing")
            if package in focus and "isKeyguardShowing=true" not in keyguard:
                time.sleep(3)  # and it stays there
                if package in self.shell(target, "dumpsys window | grep mCurrentFocus"):
                    return
        raise AssertionError(f"{component} never got input focus: {focus}")

    def wait_boot_completed(self, target: str, timeout: float = 300) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.shell(target, "getprop sys.boot_completed").strip() == "1":
                return
            time.sleep(2)
        raise AssertionError(f"{target} never reported sys.boot_completed=1")


def tcp_banner(host: str, port: int, timeout: float = 5) -> str:
    """Read the fake emulator's adb-port banner (its Pod hostname)."""
    import socket

    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        return s.recv(256).decode().strip()


# ---------------------------------------------------------------- fake-emulator events


def fake_events(kube: Kube, pod: str) -> list[dict]:
    events = []
    for line in kube.logs(pod).splitlines():
        if line.startswith("EVENT "):
            events.append(json.loads(line[6:]))
    return events
