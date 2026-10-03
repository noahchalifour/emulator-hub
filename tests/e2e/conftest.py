"""Fixtures for the end-to-end suite. Requires a cluster from e2e/up.sh; see
e2e/README.md. Every test in this directory is marked `e2e` automatically."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from tests.e2e.hub import (
    NS,
    STATE,
    UI_HOST,
    Adb,
    Env,
    HubControl,
    Kube,
    McpError,
    McpHolder,
    ui_client,
    wait_until,
)

# Small enough that E2E_SLOTS of them fit on one 4 vCPU / 16 GB CI runner.
E2E_PROFILE = "e2e"


def pytest_collection_modifyitems(config, items):
    here = Path(__file__).parent
    for item in items:
        if Path(str(item.fspath)).is_relative_to(here):
            item.add_marker(pytest.mark.e2e)
        if item.get_closest_marker("real_emulator"):
            item.add_marker(pytest.mark.android)
    # Disruptive tests (hub kills, node loss) run after everything else.
    items.sort(key=lambda i: i.get_closest_marker("disruptive") is not None)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    setattr(item, f"rep_{rep.when}", rep)
    return rep


@pytest.fixture(scope="session")
def env() -> Env:
    try:
        return Env.load()
    except FileNotFoundError as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="session")
def kube(env) -> Kube:
    return Kube(env)


@pytest.fixture(scope="session")
def hubctl(kube) -> HubControl:
    return HubControl(kube)


@pytest.fixture(autouse=True)
def _real_only(request, env):
    if request.node.get_closest_marker("real_emulator") and not env.real:
        pytest.skip("needs the real emulator image (E2E_EMULATOR=real)")
    if request.node.get_closest_marker("fake_only") and env.real:
        pytest.skip("asserts on the fake emulator's event log")


@pytest.fixture(scope="session")
def e2e_profiles(env):
    """The small profile most tests lease, created through the UI API."""
    body = {
        "form_factor": "phone",
        "system_image": "android-35-google-apis",
        "device": "medium_phone",
        # A 1-core emulator hangs ("QEMU2 CPU0 thread") on a 4 vCPU CI runner.
        "ram_mb": 2048 if env.real else 1024,
        "cores": 2 if env.real else 1,
    }
    with httpx.Client(base_url=f"http://{UI_HOST}", cookies={"e2e_user": "e2e-setup"}, timeout=60) as ui:
        r = ui.put(f"/api/profiles/{E2E_PROFILE}", json=body)
        assert r.status_code == 200, r.text
    return {E2E_PROFILE: body}


@pytest.fixture
async def mcp(env, e2e_profiles, hub_env):
    # Depends on hub_env so a test's hub reconfiguration is torn down after
    # (and its restart never strands) this session.
    holder = McpHolder(env)
    m = await holder.__aenter__()
    try:
        yield m
    finally:
        await holder.__aexit__()


@pytest.fixture
def profile(e2e_profiles) -> str:
    return E2E_PROFILE


@pytest.fixture
def holder(request) -> str:
    return "e2e:" + re.sub(r"[^\w.-]", "_", request.node.name)[:100]


@pytest.fixture
def adb(tmp_path_factory) -> Adb:
    # Short path: adb's server socket path has a length limit.
    return Adb(Path(tempfile.mkdtemp(prefix="adb", dir="/tmp")))


@pytest.fixture(scope="session")
def hub_baseline(hubctl, env):
    """The hub env as e2e/up.sh deployed it (a killed earlier run may have left
    a test's override behind, so derive it from the env file, not the cluster)."""
    current = hubctl.env_of()
    baseline = {**current, "HUB_EMULATOR_IMAGE": env.emulator_image, "HUB_SLOT_IPS": ",".join(env.slot_ips),
                "HUB_BOOT_TIMEOUT_S": str(int(env.boot_timeout_s)), "HUB_REAP_INTERVAL_S": "2"}  # fmt: skip
    baseline.pop("HUB_MAX_AGE_S", None)
    drift = {k: v for k, v in baseline.items() if current.get(k) != v}
    drift |= {k: None for k in current if k not in baseline}
    if drift:
        hubctl.set_env(**drift)
    return baseline


@pytest.fixture
def hub_env(hubctl, hub_baseline):
    """Change hub env vars for one test; the baseline is restored after."""
    changed: set[str] = set()

    def set_env(**values):
        changed.update(values)
        hubctl.set_env(**values)

    yield set_env
    if changed:
        hubctl.set_env(**{k: hub_baseline.get(k) for k in changed})


async def _release_everything(env) -> list[str]:
    """Release every active lease until the hub settles. A caller that gave up
    while queued can still be granted a slot later (ENG-336), so loop."""
    released: list[str] = []
    deadline = time.monotonic() + 180
    async with McpHolder(env) as m:
        while time.monotonic() < deadline:
            st = await m.call("status")
            busy = [s["lease_id"] for s in st["slots"] if s["lease_id"]]
            for lease_id in busy:
                try:
                    await m.call("release", lease_id=lease_id)
                    released.append(lease_id)
                except McpError:
                    pass
            if not busy and st["queue_depth"] == 0:
                # A slot can sit in `booting` with no lease for an instant during hand-off.
                if all(s["state"] == "free" for s in st["slots"]):
                    break
            await asyncio.sleep(1)
    return released


async def check_invariants(env, kube) -> None:
    """The steady state every test must leave behind."""
    async with ui_client("e2e-invariants") as ui:

        async def settled():
            st = (await ui.get("/api/status")).json()
            pods = kube.emulator_pods()
            return st["queue_depth"] == 0 and all(s["state"] == "free" for s in st["slots"]) and not pods

        try:
            await wait_until(settled, 90, interval=1, what="hub to settle")
            active = [lease for lease in (await ui.get("/api/leases?limit=500")).json() if lease["state"] != "ended"]
            assert not active, f"leases still active with every slot free: {active}"
        except AssertionError as exc:
            st = (await ui.get("/api/status")).json()
            pods = [p["metadata"]["name"] for p in kube.emulator_pods()]
            # Report, then reset so one broken test does not cascade into the rest.
            for name in pods:
                kube.delete_pod(name, grace=0)
            active = [r["id"] for r in (await ui.get("/api/leases?limit=500")).json() if r["state"] != "ended"]
            if active:
                ids = ",".join(f"'{i}'" for i in active)
                HubControl(kube).scale(0)
                HubControl(kube).sql(f"UPDATE leases SET state='ended', end_reason='lost' WHERE id IN ({ids})")
                HubControl(kube).scale(1)
            raise AssertionError(f"invariants violated: {exc}; status={st} emulator_pods={pods}") from None
        assert len((await ui.get("/api/status")).json()["slots"]) == len(env.slot_ips)


def _dump_artifacts(request, kube) -> None:
    out = artifact_dir(request)
    out.mkdir(parents=True, exist_ok=True)
    try:
        hub = kube.run("-n", NS, "get", "pods", "-l", "app.kubernetes.io/name=emulator-hub", "-o", "name", check=False)
        for name in hub.split():
            (out / f"{name.replace('/', '_')}.log").write_text(kube.run("-n", NS, "logs", name, check=False))
        for p in kube.emulator_pods():
            name = p["metadata"]["name"]
            (out / f"{name}.log").write_text(kube.logs(name))
        (out / "events.txt").write_text(kube.run("-n", NS, "get", "events", "--sort-by=.lastTimestamp", check=False))
        (out / "describe.txt").write_text(kube.run("-n", NS, "describe", "pods", check=False))
    except Exception as exc:  # artifacts are best effort
        (out / "artifact-error.txt").write_text(repr(exc))


class PodLogTail:
    """Follow every emulator Pod's log for the length of a test: the hub deletes
    a failed Pod at once, so logs fetched afterwards are already gone."""

    def __init__(self, kube, out_dir):
        self.kube, self.out = kube, out_dir
        self.procs: dict[str, subprocess.Popen] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def _watch(self):
        while not self._stop.is_set():
            try:
                names = {p["metadata"]["name"] for p in self.kube.emulator_pods()}
            except Exception:
                names = set()
            try:
                for p in self.kube.emulator_pods():
                    for cs in p.get("status", {}).get("containerStatuses", []):
                        term = cs.get("state", {}).get("terminated") or cs.get("lastState", {}).get("terminated")
                        if term:
                            self.out.mkdir(parents=True, exist_ok=True)
                            with open(self.out / "terminations.txt", "a") as f:
                                f.write(f"{p['metadata']['name']}: {term.get('reason')} exit={term.get('exitCode')}\n")
            except Exception:
                pass
            for name in names - set(self.procs):
                self.out.mkdir(parents=True, exist_ok=True)
                f = open(self.out / f"{name}.stream.log", "w")  # noqa: SIM115 - closed with the process
                self.procs[name] = subprocess.Popen(
                    ["kubectl", "--context", self.kube.env.context, "-n", NS, "logs", "-f", "--pod-running-timeout=60s",
                     name],
                    stdout=f, stderr=subprocess.STDOUT,
                )  # fmt: skip
            self._stop.wait(1)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(5)
        for p in self.procs.values():
            p.terminate()


def artifact_dir(request):
    return STATE / "artifacts" / re.sub(r"[^\w.-]", "_", request.node.nodeid)[-150:]


@pytest.fixture(autouse=True)
async def invariants(request, env, kube):
    tail = PodLogTail(kube, artifact_dir(request) / "stream") if env.real else None
    if tail:
        tail.__enter__()
    yield
    if tail:
        tail.__exit__()
    failed = getattr(request.node, "rep_call", None) is not None and request.node.rep_call.failed
    keep = failed or bool(os.environ.get("E2E_ARTIFACTS_ALWAYS"))
    if keep:
        _dump_artifacts(request, kube)
    elif tail:
        import shutil

        shutil.rmtree(artifact_dir(request) / "stream", ignore_errors=True)
    try:
        await _release_everything(env)
        await check_invariants(env, kube)
    except BaseException:
        _dump_artifacts(request, kube)
        raise
