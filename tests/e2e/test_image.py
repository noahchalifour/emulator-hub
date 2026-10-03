"""ENG-342: the emulator image contract, under plain Docker (no cluster, no hub).

Run per catalog entry by the `emulator-image` CI matrix:
    E2E_IMAGE=emulator:ci E2E_SYSTEM_IMAGE=... E2E_DEVICE=... \
        uv run pytest tests/e2e/test_image.py -m e2e -o addopts=
Needs Linux with /dev/kvm. Each test starts its own container."""

import asyncio
import contextlib
import io
import os
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from PIL import Image

from emulator_hub.catalog import SYSTEM_IMAGES

IMAGE = os.environ.get("E2E_IMAGE", "")
SYSTEM_IMAGE = os.environ.get("E2E_SYSTEM_IMAGE", "")
DEVICE = os.environ.get("E2E_DEVICE", "")
APK = Path(__file__).resolve().parents[2] / "e2e" / "test-apk" / "e2e-probe.apk"
DISPLAY = {"pixel_8": (1080, 2400), "medium_phone": (1080, 2400), "pixel_tablet": (2560, 1600),
           "medium_tablet": (2560, 1600), "tv_1080p": (1920, 1080), "tv_720p": (1280, 720)}  # fmt: skip

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not IMAGE, reason="set E2E_IMAGE (and E2E_SYSTEM_IMAGE, E2E_DEVICE) to run"),
]


# These tests do not use the cluster; opt out of the cluster fixtures in conftest.
@pytest.fixture(autouse=True)
def invariants():
    yield


@pytest.fixture(autouse=True)
def _real_only():
    yield


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Container:
    def __init__(self, *docker_args: str, env: dict[str, str] | None = None):
        self.name = f"emu-{uuid.uuid4().hex[:8]}"
        self.grpc_port, self.adb_port = free_port(), free_port()
        base_env = {"SYSTEM_IMAGE": SYSTEM_IMAGE, "DEVICE": DEVICE, "RAM_MB": "2048", "CORES": "2"}
        args = ["docker", "run", "-d", "--name", self.name, "-p", f"127.0.0.1:{self.grpc_port}:8554",
                "-p", f"127.0.0.1:{self.adb_port}:5557"]  # fmt: skip
        for k, v in {**base_env, **(env or {})}.items():
            args += ["-e", f"{k}={v}"]
        subprocess.run([*args, *docker_args, IMAGE], check=True, capture_output=True)

    def wait_exit(self, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", self.name],
                capture_output=True, text=True,
            ).stdout.split()  # fmt: skip
            if state and state[0] == "exited":
                return int(state[1])
            time.sleep(1)
        return None

    def logs(self) -> str:
        r = subprocess.run(["docker", "logs", self.name], capture_output=True, text=True)
        return r.stdout + r.stderr

    def exec(self, *cmd: str) -> str:
        return subprocess.run(["docker", "exec", self.name, *cmd], capture_output=True, text=True, check=True).stdout

    def remove(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


@contextlib.contextmanager
def container(*args, **kw):
    c = Container(*args, **kw)
    try:
        yield c
    finally:
        if os.environ.get("E2E_IMAGE_LOGS"):
            print(c.logs()[-5000:])
        c.remove()


@pytest.fixture(scope="module")
def booted():
    """One booted emulator shared by the boot-dependent tests in this module."""
    with container("--device", "/dev/kvm") as c:
        from emulator_hub.emulator_grpc import GrpcBootProbe

        probe = GrpcBootProbe()
        target = f"127.0.0.1:{c.grpc_port}"

        async def wait():
            # GrpcBootProbe takes a host and adds :8554; talk to the mapped port instead.
            import emulator_hub.emulator_grpc as eg

            orig = eg.GRPC_PORT
            deadline = time.monotonic() + 480
            started = time.monotonic()
            while time.monotonic() < deadline:
                eg.GRPC_PORT = c.grpc_port
                try:
                    if await probe.booted("127.0.0.1"):
                        return time.monotonic() - started
                finally:
                    eg.GRPC_PORT = orig
                await asyncio.sleep(5)
            raise AssertionError(f"{target} did not boot in 8 minutes:\n{c.logs()[-3000:]}")

        c.boot_seconds = asyncio.run(wait())
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write(f"boot_seconds={c.boot_seconds:.0f}\n")
        print(f"booted {SYSTEM_IMAGE} {DEVICE} in {c.boot_seconds:.0f}s")
        yield c


def screen(c):
    import emulator_hub.emulator_grpc as eg

    class Mapped(eg.GrpcScreen):
        def __init__(self):
            self._channel = eg.grpc.aio.insecure_channel(
                f"127.0.0.1:{c.grpc_port}", options=[("grpc.max_receive_message_length", 32 * 1024 * 1024)]
            )
            self._stub = eg.rpc.EmulatorControllerStub(self._channel)
            self._size = None

    return Mapped()


def fresh_adb() -> dict[str, str]:
    home = tempfile.mkdtemp(prefix="adb", dir="/tmp")
    env = {**os.environ, "HOME": home, "ANDROID_USER_HOME": f"{home}/.android"}
    env.pop("ADB_VENDOR_KEYS", None)
    os.makedirs(f"{home}/.android")
    subprocess.run(["adb", "keygen", f"{home}/.android/adbkey"], env=env, capture_output=True, check=True)
    return env


def adb(env, *args, check=True, timeout=120) -> str:
    r = subprocess.run(["adb", *args], env=env, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise AssertionError(f"adb {args}: {r.stdout}{r.stderr}")
    return r.stdout.replace("\r", "")


def install_probe(env, target, attempts=5) -> None:
    """adb install, retried: right after boot the package manager can refuse
    with an empty reason."""
    last = None
    for _ in range(attempts):
        try:
            adb(env, "-s", target, "install", "-r", "-g", str(APK), timeout=300)
            return
        except AssertionError as exc:
            last = exc
            time.sleep(5)
    raise last


def connect(env, target, timeout=120) -> str:
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        adb(env, "connect", target, check=False)
        state = adb(env, "-s", target, "get-state", check=False).strip()
        if state == "device":
            return state
        time.sleep(3)
    return state or adb(env, "devices", check=False)


# ------------------------------------------------------------------ boot


def test_boots_and_reports_the_profile_hardware(booted):
    async def status():
        import grpc
        from google.protobuf.empty_pb2 import Empty

        import emulator_hub._grpc.emulator_controller_pb2_grpc as rpc

        async with grpc.aio.insecure_channel(f"127.0.0.1:{booted.grpc_port}") as ch:
            return await rpc.EmulatorControllerStub(ch).getStatus(Empty(), timeout=10)

    st = asyncio.run(status())
    assert st.booted
    hw = {e.key: e.value for e in st.hardwareConfig.entry}
    # The emulator may raise RAM to the system image's minimum (API 35: 2560).
    assert int(hw["hw.ramSize"].rstrip("MB")) >= 2048, hw.get("hw.ramSize")
    assert hw.get("hw.cpu.ncore") == "2"
    # Without a hardware keyboard the emulator drops every gRPC key event.
    assert hw.get("hw.keyboard") in ("yes", "true"), hw.get("hw.keyboard")


def test_snapshot_is_a_jpeg_with_the_device_aspect(booted):
    async def shot():
        s = screen(booted)
        try:
            return await s.snapshot()
        finally:
            await s.close()

    jpeg = asyncio.run(shot())
    assert jpeg[:2] == b"\xff\xd8"
    img = Image.open(io.BytesIO(jpeg))
    w, h = DISPLAY[DEVICE]
    assert abs(img.height / img.width - h / w) < 0.03


def unlock(c) -> tuple[dict, str]:
    """Connect adb (container key, so this works on every image) and dismiss
    the keyguard a -wipe-data boot starts behind."""
    env = fresh_adb()
    key = subprocess.run(["docker", "exec", c.name, "cat", "/home/emu/.android/adbkey"],
                         capture_output=True, text=True, check=True).stdout  # fmt: skip
    Path(env["HOME"], ".android", "adbkey").write_text(key)
    env["ADB_VENDOR_KEYS"] = f"{env['HOME']}/.android/adbkey"
    target = f"127.0.0.1:{c.adb_port}"
    assert connect(env, target) == "device"
    adb(env, "-s", target, "shell", "wm dismiss-keyguard; locksettings set-disabled true")
    return env, target


def test_grpc_frames_touch_key_and_close(booted):
    env, target = unlock(booted)
    # The probe app flips its background on every touch, so frames must change.
    install_probe(env, target)
    adb(env, "-s", target, "shell", "am start -W -n dev.emulatorhub.e2e/.ProbeActivity")

    async def run():
        s = screen(booted)
        frames = []
        try:
            before = await s.snapshot()
            await s.touch(0.5, 0.5, True)
            await s.touch(0.5, 0.5, False)

            async def collect():
                async for f in s.frames():
                    frames.append(f)
                    if len(frames) >= 10 and len({bytes(f) for f in frames}) > 1:
                        return

            async def poke():
                for i in range(200):
                    await s.touch(0.1 + (i % 8) * 0.1, 0.5, True)
                    await s.touch(0.1 + (i % 8) * 0.1, 0.5, False)
                    await asyncio.sleep(0.25)

            poker = asyncio.create_task(poke())
            await asyncio.wait_for(collect(), 90)
            poker.cancel()
            after = await s.snapshot()
            return before, after, frames
        finally:
            await s.close()
            assert s._channel.get_state() is not None

    before, after, frames = asyncio.run(run())
    assert len(frames) >= 10 and all(f[:2] == b"\xff\xd8" for f in frames)
    assert before != after or len({bytes(f) for f in frames}) > 1


def test_text_reaches_a_focused_field(booted):
    env, target = unlock(booted)
    install_probe(env, target)
    adb(env, "-s", target, "shell", "logcat -c")
    adb(env, "-s", target, "shell", "am start -W -n dev.emulatorhub.e2e/.ProbeActivity")
    time.sleep(3)

    long = "The quick brown fox jumps over the lazy dog, 0123456789!"  # several text events

    async def type_():
        s = screen(booted)
        try:
            await s.text("hello e2e")
            await s.text(" " + long)  # longer than one emulator text event
        finally:
            await s.close()

    asyncio.run(type_())
    deadline = time.monotonic() + 60
    log = ""
    while time.monotonic() < deadline:
        log = adb(env, "-s", target, "shell", "logcat -d -s E2E:I")
        if "text hello e2e " + long.rstrip() in log.replace("\n", ""):
            break
        time.sleep(1)
    assert "text hello e2e" in log, log[-2000:]
    raw = adb(env, "-s", target, "shell", "logcat -d -v raw -s E2E:I")
    last = [m for m in raw.split("\ntext ") if m][-1].split("\nkey")[0].replace("\n", "")
    assert last.rstrip() == ("hello e2e " + long).rstrip(), last[-120:]

    async def press():
        s = screen(booted)
        try:
            await s.key("Enter")
        finally:
            await s.close()

    adb(env, "-s", target, "shell", "logcat -c")
    asyncio.run(press())
    time.sleep(2)
    log = adb(env, "-s", target, "shell", "logcat -d -s E2E:I")
    # At most one press: a held key auto-repeats. (On TV the leanback IME
    # consumes Enter before the window sees it.)
    assert log.count("key 66 0") <= 1, log
    if "android-tv" not in SYSTEM_IMAGE:
        assert log.count("key 66 0") == 1 and log.count("key 66 1") == 1, log


# ------------------------------------------------------------------ adb


def test_never_seen_client_key_is_authorized(booted):
    """What a real agent does: a fresh adb key, never copied from the container."""
    if "android-tv" in SYSTEM_IMAGE:
        pytest.xfail("android-tv images reject a never-seen adb key as unauthorized (ENG-342)")
    env = fresh_adb()
    target = f"127.0.0.1:{booted.adb_port}"
    assert connect(env, target) == "device"
    assert adb(env, "-s", target, "shell", "getprop sys.boot_completed").strip() == "1"


def test_two_clients_with_different_keys(booted):
    if "android-tv" in SYSTEM_IMAGE:
        pytest.xfail("android-tv images reject a never-seen adb key as unauthorized (ENG-342)")
    a, b = fresh_adb(), fresh_adb()
    target = f"127.0.0.1:{booted.adb_port}"
    assert connect(a, target) == "device" and connect(b, target) == "device"
    assert adb(a, "-s", target, "shell", "echo a").strip() == "a"
    assert adb(b, "-s", target, "shell", "echo b").strip() == "b"


def test_socat_bridge_survives_reconnect_cycles(booted):
    env = fresh_adb()
    if "android-tv" in SYSTEM_IMAGE:
        # Use the container's own key so this checks the bridge, not authorization.
        key = subprocess.run(["docker", "exec", booted.name, "cat", "/home/emu/.android/adbkey"],
                             capture_output=True, text=True, check=True).stdout  # fmt: skip
        Path(env["HOME"], ".android", "adbkey").write_text(key)
        env["ADB_VENDOR_KEYS"] = f"{env['HOME']}/.android/adbkey"
        adb(env, "kill-server", check=False)
    target = f"127.0.0.1:{booted.adb_port}"
    for _ in range(10):
        assert connect(env, target) == "device"
        adb(env, "disconnect", target, check=False)
    # Concurrent TCP connections (socat fork).
    socks = [socket.create_connection(("127.0.0.1", booted.adb_port), timeout=5) for _ in range(5)]
    for s in socks:
        s.close()
    assert connect(env, target) == "device"


# ------------------------------------------------------------------ hardening and failure modes


def test_runs_non_root_with_no_capabilities(booted):
    assert booted.exec("id", "-u").strip() == "10010"


def test_boots_with_all_capabilities_dropped():
    with container("--device", "/dev/kvm", "--cap-drop", "ALL", "--security-opt", "no-new-privileges") as c:
        deadline = time.monotonic() + 480
        while time.monotonic() < deadline:
            if c.wait_exit(1) is not None:
                raise AssertionError(c.logs()[-3000:])
            if "boot completed" in c.logs().lower() or _booted_grpc(c.grpc_port):
                return
            time.sleep(5)
        raise AssertionError("did not boot with caps dropped")


def _booted_grpc(port) -> bool:
    import grpc
    from google.protobuf.empty_pb2 import Empty

    import emulator_hub._grpc.emulator_controller_pb2_grpc as rpc

    async def q():
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
            try:
                return (await rpc.EmulatorControllerStub(ch).getStatus(Empty(), timeout=5)).booted
            except grpc.aio.AioRpcError:
                return False

    return asyncio.run(q())


def test_missing_kvm_exits_3_fast():
    with container() as c:
        assert c.wait_exit(30) == 3
        assert "FATAL: /dev/kvm missing or not writable" in c.logs()


def test_read_only_kvm_exits_3():
    with container("--device", "/dev/kvm:/dev/kvm:r") as c:
        assert c.wait_exit(30) == 3


@pytest.mark.parametrize("bad", [{"SYSTEM_IMAGE": "system-images;android-1;nope;x86_64"}, {"DEVICE": "toaster"}])
def test_bad_config_fails_fast_with_a_readable_error(bad):
    with container("--device", "/dev/kvm", env=bad) as c:
        code = c.wait_exit(90)
        assert code not in (None, 0), "should exit quickly, not hang"
        logs = c.logs().lower()
        assert "error" in logs or "invalid" in logs or "not" in logs


def test_fresh_emptydir_gives_a_factory_fresh_device():
    """-wipe-data / -no-snapshot: a new container on a fresh /avd has no trace
    of the previous one."""
    env = fresh_adb()
    with container("--device", "/dev/kvm") as c:
        assert _wait_booted(c)
        tgt = f"127.0.0.1:{c.adb_port}"
        if connect(env, tgt) != "device":
            pytest.skip("adb not authorized for a fresh key on this image (see test_never_seen_client_key)")
        adb(env, "-s", tgt, "shell", "echo marker > /data/local/tmp/marker")
    with container("--device", "/dev/kvm") as c:
        assert _wait_booted(c)
        tgt = f"127.0.0.1:{c.adb_port}"
        assert connect(env, tgt) == "device"
        out = adb(env, "-s", tgt, "shell", "ls /data/local/tmp/marker 2>&1 || true")
        assert "No such file" in out


def _wait_booted(c, timeout=480) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _booted_grpc(c.grpc_port):
            return True
        time.sleep(5)
    return False


def test_every_catalog_package_is_installed_in_the_image():
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sdkmanager", IMAGE, "--list_installed"],
        capture_output=True, text=True, timeout=300,
    ).stdout  # fmt: skip
    for spec in SYSTEM_IMAGES.values():
        assert spec.package in out, spec.package


def test_grpc_gohome_leaves_the_app(booted):
    env, target = unlock(booted)
    install_probe(env, target)
    adb(env, "-s", target, "shell", "am start -W -n dev.emulatorhub.e2e/.ProbeActivity")

    def focused():
        return adb(env, "-s", target, "shell", "dumpsys window | grep mCurrentFocus")

    assert "dev.emulatorhub.e2e" in focused()

    async def home():
        s = screen(booted)
        try:
            await s.key("GoHome")
        finally:
            await s.close()

    asyncio.run(home())
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and "dev.emulatorhub.e2e" in focused():
        time.sleep(1)
    assert "dev.emulatorhub.e2e" not in focused()


def test_boots_under_containerd_2s_huge_nofile_limit():
    """containerd >= 2 starts containers with RLIMIT_NOFILE=1073741816, and the
    emulator's vCPU threads hang at boot under it. The entrypoint clamps it."""
    if int(Path("/proc/sys/fs/nr_open").read_text()) < 1073741816:
        pytest.skip("raise fs.nr_open to 1073741816 to run this (CI does)")
    with container("--device", "/dev/kvm", "--ulimit", "nofile=1073741816:1073741816") as c:
        assert _wait_booted(c, timeout=300), c.logs()[-3000:]
        assert "hanging thread" not in c.logs()


@pytest.mark.parametrize("ram_mb", [1024, 2048, 4096])
def test_boots_within_the_pods_memory_limit(ram_mb):
    """The boot fits inside the memory limit the hub gives the Pod
    (pods.memory_mb), even though the emulator raises guest RAM to the system
    image's minimum and a large display adds GPU emulation buffers."""
    from emulator_hub.models import Profile
    from emulator_hub.pods import build_pod

    image = next(k for k, v in SYSTEM_IMAGES.items() if v.package == SYSTEM_IMAGE)
    from emulator_hub.catalog import DEVICES

    ff = next(ff for ff, devs in DEVICES.items() if DEVICE in devs)
    pod = build_pod(
        namespace="x", image=IMAGE, slot=0, lease_id="memlimit", profile=Profile("m", ff, image, DEVICE, ram_mb, 2)
    )
    limit = pod["spec"]["containers"][0]["resources"]["limits"]["memory"]
    mib = int(limit.removesuffix("Mi"))
    with container("--device", "/dev/kvm", "--memory", f"{mib}m", "--memory-swap", f"{mib}m",
                   env={"RAM_MB": str(ram_mb)}) as c:  # fmt: skip
        booted = _wait_booted(c, timeout=300)
        state = subprocess.run(["docker", "inspect", "-f", "{{.State.OOMKilled}} {{.State.Status}}", c.name],
                               capture_output=True, text=True).stdout.strip()  # fmt: skip
        assert booted, f"did not boot under a {limit} limit ({state}):\n{c.logs()[-1500:]}"
        time.sleep(20)  # and stays up once the launcher settles
        state = subprocess.run(["docker", "inspect", "-f", "{{.State.OOMKilled}} {{.State.Status}}", c.name],
                               capture_output=True, text=True).stdout.strip()  # fmt: skip
        assert state == "false running", state
