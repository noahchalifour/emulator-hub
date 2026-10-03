"""ENG-339: /api/leases/{id}/live and /snapshot against a real gRPC bridge.

With the fake emulator, injected input is read back from its event log; with
the real emulator, from the probe APK's logcat (tag E2E) via adb."""

import asyncio
import io
import json
import re
import time

import pytest
import websockets
import websockets.exceptions
from PIL import Image
from playwright.async_api import async_playwright, expect

from tests.e2e.hub import (
    APK,
    UI_HOST,
    acquire_leased,
    fake_events,
    pod_name,
    ui_client,
    wait_until,
    ws_url,
)

COOKIE = {"Cookie": "e2e_user=noah"}
ASPECT = {"phone": 2400 / 1080, "tablet": 1600 / 2560, "tv": 1080 / 1920}


def open_live(lease_id, **kw):
    return websockets.connect(ws_url(lease_id), additional_headers=COOKIE, max_size=None, **kw)


async def recv_frames(ws, n, timeout=60):
    frames = []
    deadline = time.monotonic() + timeout
    while len(frames) < n:
        msg = await asyncio.wait_for(ws.recv(), max(0.1, deadline - time.monotonic()))
        assert isinstance(msg, bytes), msg
        frames.append(msg)
    return frames


class InputLog:
    """Where injected input shows up: the fake's event log, or logcat on a real device."""

    def __init__(self, env, kube, adb, grant):
        self.env, self.kube, self.adb, self.grant = env, kube, adb, grant
        self.pod = pod_name(grant["slot"], grant["lease_id"])
        self.target = grant["adb"]
        if env.real:
            adb.connect(self.target)
            adb.wait_boot_completed(self.target)
            adb.install(self.target, APK)
            adb.focus_app(self.target, "dev.emulatorhub.e2e/.ProbeActivity")
            adb.shell(self.target, "logcat -c")

    def size(self) -> tuple[int, int]:
        if not self.env.real:
            started = next(e for e in fake_events(self.kube, self.pod) if e["type"] == "started")
            assert started
            from tests.e2e.test_lifecycle import DISPLAY

            return DISPLAY[started["device"]]
        m = re.search(r"(\d+)x(\d+)", self.adb.shell(self.target, "wm size").splitlines()[-1])
        return int(m.group(1)), int(m.group(2))

    def touches(self) -> list[tuple[int, int, int]]:
        """(x, y, down) in display pixels."""
        if not self.env.real:
            return [
                (e["x"], e["y"], 1 if e["pressure"] else 0)
                for e in fake_events(self.kube, self.pod)
                if e["type"] == "touch"
            ]
        out = self.adb.shell(self.target, "logcat -d -s E2E:I")
        res = []
        for m in re.finditer(r"touch (\d+) (\d+) (\d+)", out):
            action, x, y = map(int, m.groups())
            if action in (0, 1):  # ACTION_DOWN / ACTION_UP
                res.append((x, y, 1 if action == 0 else 0))
        return res

    def keys(self) -> list[str]:
        if not self.env.real:
            return [e["key"] for e in fake_events(self.kube, self.pod) if e["type"] == "key" and e["key"]]
        return re.findall(r"key (\d+) 0", self.adb.shell(self.target, "logcat -d -s E2E:I"))

    def text(self) -> str:
        if not self.env.real:
            return "".join(e["text"] for e in fake_events(self.kube, self.pod) if e["type"] == "key")
        # -v raw: no per-line prefixes, so a long field logged across several
        # logcat lines can be rejoined.
        raw = self.adb.shell(self.target, "logcat -d -v raw -s E2E:I")
        texts = [m for m in re.split(r"\n(?=text |touch |key |ready)", raw) if m.startswith("text ")]
        return texts[-1][5:].replace("\n", "") if texts else ""

    def open_streams(self) -> int:
        if self.env.real:
            return -1
        ev = fake_events(self.kube, self.pod)
        return sum(e["type"] == "stream_open" for e in ev) - sum(e["type"] == "stream_closed" for e in ev)


@pytest.fixture
async def leased(mcp, env, profile, holder):
    return await acquire_leased(mcp, env, profile, holder)


@pytest.fixture
def inputs(env, kube, adb, leased):
    return InputLog(env, kube, adb, leased)


# ------------------------------------------------------------------ snapshot


@pytest.mark.android
@pytest.mark.parametrize(
    "form_factor,device", [("phone", "medium_phone"), ("tablet", "medium_tablet"), ("tv", "tv_720p")]
)
async def test_snapshot_is_a_small_jpeg_with_the_device_aspect(env, mcp, holder, form_factor, device):
    image = "android-36-android-tv" if form_factor == "tv" else "android-35-google-apis"
    body = {"form_factor": form_factor, "system_image": image, "device": device,
            "ram_mb": 2048 if env.real else 1024, "cores": 2 if env.real else 1}  # fmt: skip
    async with ui_client() as ui:
        assert (await ui.put(f"/api/profiles/snap-{form_factor}", json=body)).status_code == 200
        try:
            grant = await acquire_leased(mcp, env, f"snap-{form_factor}", holder)
            r = await ui.get(f"/api/leases/{grant['lease_id']}/snapshot")
            assert r.status_code == 200
            assert r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"] == "no-store"
            assert r.content[:2] == b"\xff\xd8"
            img = Image.open(io.BytesIO(r.content))
            assert abs(img.width - 240) <= 2, img.width  # the emulator rounds the scaled size
            assert abs(img.height / img.width - ASPECT[form_factor]) < 0.02
            await mcp.call("release", lease_id=grant["lease_id"])
        finally:
            await ui.delete(f"/api/profiles/snap-{form_factor}")


async def test_snapshot_404s_for_unknown_ended_and_booting(env, mcp, hub_env, profile, holder):
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:slow")
    booting = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    ended = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    await mcp.call("release", lease_id=ended["lease_id"])
    async with ui_client() as ui:
        for lease_id in ("nope", ended["lease_id"], booting["lease_id"]):
            r = await ui.get(f"/api/leases/{lease_id}/snapshot")
            assert r.status_code == 404 and r.json()["detail"] == "that lease has no running emulator"


async def _snapshot_of_a_dead_emulator(env, mcp, kube, hub_env, profile, holder):
    hub_env(HUB_REAP_INTERVAL_S="600")  # keep the reaper from noticing first
    grant = await acquire_leased(mcp, env, profile, holder)
    name = pod_name(grant["slot"], grant["lease_id"])
    kube.kill_container(name)  # the emulator process dies; the Pod object stays Failed
    await wait_until(lambda: (kube.pod(name) or {}).get("status", {}).get("phase") == "Failed", 60)
    async with ui_client(timeout=120) as ui:
        started = time.monotonic()
        r = await ui.get(f"/api/leases/{grant['lease_id']}/snapshot")
        took = time.monotonic() - started
        assert (await ui.get("/api/me")).status_code == 200  # still serving
    return r, took


async def test_snapshot_of_a_dead_emulator_is_a_502(env, mcp, kube, hub_env, profile, holder):
    r, _ = await _snapshot_of_a_dead_emulator(env, mcp, kube, hub_env, profile, holder)
    assert r.status_code == 502 and "emulator did not return a screenshot" in r.json()["detail"]


@pytest.mark.xfail(
    strict=True,
    reason="GrpcScreen._device_size calls getDisplayConfigurations with no deadline, so a dead emulator holds "
    "each snapshot request (one per slot card per 3s refresh) for ~20s until gRPC gives up (ENG-339)",
)
async def test_snapshot_of_a_dead_emulator_fails_fast(env, mcp, kube, hub_env, profile, holder):
    _, took = await _snapshot_of_a_dead_emulator(env, mcp, kube, hub_env, profile, holder)
    assert took < 10, f"{took:.1f}s"


# ------------------------------------------------------------------ websocket basics


async def _refusal(url, headers=None):
    """How a real client sees a refused live-view upgrade: (http_status, close_code)."""
    try:
        async with websockets.connect(url, additional_headers=headers or {}) as ws:
            try:
                await asyncio.wait_for(ws.recv(), 10)
            except websockets.exceptions.ConnectionClosed as exc:
                return None, exc.rcvd.code if exc.rcvd else None
            return None, None
    except websockets.exceptions.InvalidStatus as exc:
        return exc.response.status_code, None


async def test_refused_live_views(env, mcp, kube, hub_env, profile, holder, leased):
    """Unauthenticated, unknown, ended and still-booting leases are all refused
    without a session: the ingress answers 401 without authentik, and the hub
    refuses the upgrade itself for the rest."""
    from tests.e2e.hub import port_forward

    assert await _refusal(ws_url(leased["lease_id"])) == (401, None)
    with port_forward(kube, 8080) as ports:
        direct = f"ws://127.0.0.1:{ports[8080]}/api/leases/{leased['lease_id']}/live"
        status, code = await _refusal(direct)
    assert (status, code) in ((403, None), (None, 4401))
    assert await _refusal(ws_url("nope"), COOKIE) in ((403, None), (None, 4404))
    await mcp.call("release", lease_id=leased["lease_id"])
    assert await _refusal(ws_url(leased["lease_id"]), COOKIE) in ((403, None), (None, 4404))
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:slow")
    booting = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    assert await _refusal(ws_url(booting["lease_id"]), COOKIE) in ((403, None), (None, 4404))


@pytest.mark.xfail(
    strict=True,
    reason="the live view closes before accept(), which uvicorn turns into a bare HTTP 403: real clients never "
    "see the 4401/4404 close codes the UI and docs rely on to tell 'sign in' from 'lease gone' (ENG-339)",
)
async def test_close_codes_reach_real_clients(env, kube, leased):
    from tests.e2e.hub import port_forward

    with port_forward(kube, 8080) as ports:
        direct = f"ws://127.0.0.1:{ports[8080]}/api/leases/{leased['lease_id']}/live"
        assert await _refusal(direct) == (None, 4401)
    assert await _refusal(ws_url("nope"), COOKIE) == (None, 4404)


@pytest.mark.android
async def test_frames_flow_and_change_with_the_screen(env, inputs, leased):
    async with open_live(leased["lease_id"]) as ws:
        # Touch so even a static real screen produces frames.
        await ws.send(json.dumps({"t": "touch", "x": 0.5, "y": 0.5, "down": True}))
        await ws.send(json.dumps({"t": "touch", "x": 0.5, "y": 0.5, "down": False}))
        started = time.monotonic()
        frames = await recv_frames(ws, 10, timeout=30)
        if not env.real:
            assert 10 / (time.monotonic() - started) >= 5, "at least 5 fps on a changing screen"
        for f in frames:
            assert f[:2] == b"\xff\xd8"
            img = Image.open(io.BytesIO(f))
            assert img.width <= 480
        assert len({bytes(f) for f in frames}) > 1, "frames reflect screen changes"


# ------------------------------------------------------------------ input


@pytest.mark.android
async def test_touch_lands_at_the_matching_device_pixel(env, inputs, leased):
    w, h = inputs.size()
    # Inside the app window: the status and navigation bars swallow taps at the
    # very edges on a real device. (0,0)/(1,1) are covered on the fake.
    points = (
        ((0.5, 0.5), (0.0, 0.0), (1.0, 1.0), (0.25, 0.75))
        if not env.real
        else ((0.5, 0.5), (0.1, 0.2), (0.9, 0.8), (0.25, 0.75))
    )
    async with open_live(leased["lease_id"]) as ws:
        await recv_frames(ws, 1)  # the stream (and the gRPC channel) is up
        for x, y in points:
            await ws.send(json.dumps({"t": "touch", "x": x, "y": y, "down": True}))
            await ws.send(json.dumps({"t": "touch", "x": x, "y": y, "down": False}))
            await asyncio.sleep(0.5)
        # Out of range: ignored, connection stays up.
        await ws.send(json.dumps({"t": "touch", "x": -0.1, "y": 1.5, "down": True}))
        await recv_frames(ws, 1)

    def downs():
        return [(x, y) for x, y, d in inputs.touches() if d]

    try:
        got = await wait_until(lambda: len(downs()) >= 4 and downs(), 20, what="4 taps")
    except AssertionError:
        extra = ""
        if env.real:
            a, t = inputs.adb, inputs.target
            extra = (f"\nfocus: {a.shell(t, 'dumpsys window | grep -E \"mCurrentFocus|isKeyguardShowing\"')}"
                     f"\nlogcat E2E: {a.shell(t, 'logcat -d -s E2E:I')[-1500:]}"
                     f"\ngetevent devices: {a.shell(t, 'getevent -p 2>/dev/null | grep -E \"name:\"')}")  # fmt: skip
        raise AssertionError(f"taps seen: {downs()}; display {w}x{h}{extra}") from None
    expected = list(points)
    assert len(got) == 4, got
    for (gx, gy), (ex, ey) in zip(got, expected, strict=True):
        assert abs(gx - ex * w) <= 0.02 * w + 1 and abs(gy - ey * h) <= 0.02 * h + 1, (gx, gy, ex * w, ey * h)


@pytest.mark.android
async def test_drag_is_a_down_moves_up_sequence(env, inputs, leased):
    async with open_live(leased["lease_id"]) as ws:
        for i in range(6):
            await ws.send(json.dumps({"t": "touch", "x": 0.2 + i * 0.1, "y": 0.5, "down": True}))
            await asyncio.sleep(0.05)
        await ws.send(json.dumps({"t": "touch", "x": 0.7, "y": 0.5, "down": False}))
        await recv_frames(ws, 1)
    if env.real:
        log = inputs.adb.shell(inputs.target, "logcat -d -s E2E:I")
        actions = [int(a) for a in re.findall(r"touch (\d+) ", log)]
        assert actions[0] == 0 and 2 in actions and actions[-1] == 1, actions  # DOWN, MOVE..., UP
    else:
        seq = await wait_until(lambda: len(inputs.touches()) >= 7 and inputs.touches(), 20)
        assert [d for *_, d in seq[-7:]] == [1, 1, 1, 1, 1, 1, 0]
        xs = [x for x, *_ in seq[-7:-1]]
        assert xs == sorted(xs) and xs[0] < xs[-1]


# Android KEYCODE_* per DOM key name (what the probe app logs).
KEYCODES = {
    "GoHome": 3, "GoBack": 4, "AppSwitch": 187, "Power": 26, "AudioVolumeUp": 24, "AudioVolumeDown": 25,
    "Enter": 66, "Backspace": 67, "ArrowUp": 19, "ArrowDown": 20, "ArrowLeft": 21, "ArrowRight": 22,
}  # fmt: skip


@pytest.mark.android
async def test_window_keys_reach_the_device_and_unknown_keys_do_not(env, inputs, leased):
    # Keys the app window sees (Home/Recents/Power/Volume are consumed by the system).
    keys = ["Enter", "Backspace", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "GoBack"]
    async with open_live(leased["lease_id"]) as ws:
        for k in keys + ["rm -rf /", "F13"]:
            await ws.send(json.dumps({"t": "key", "key": k}))
            await asyncio.sleep(0.3)
        await recv_frames(ws, 1)
    expected = [str(KEYCODES[k]) for k in keys] if env.real else keys
    got = await wait_until(lambda: len(inputs.keys()) >= len(keys) and inputs.keys(), 20, what="keys")
    assert got[: len(keys)] == expected
    assert "rm -rf /" not in got and "F13" not in got


@pytest.mark.real_emulator
async def test_system_keys_have_their_system_effect(env, inputs, leased):
    t = inputs.target
    adb = inputs.adb

    async def send(key):
        async with open_live(leased["lease_id"]) as ws:
            await ws.send(json.dumps({"t": "key", "key": key}))
            await asyncio.sleep(2)

    def focused():
        return adb.shell(t, "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -1")

    def awake():
        return adb.shell(t, "dumpsys power | grep mWakefulness=")

    assert "dev.emulatorhub.e2e" in focused()
    # Volume keys are offered to the focused window before the system acts on
    # them (KEYCODE_VOLUME_UP/DOWN = 24/25).
    await send("AudioVolumeUp")
    await send("AudioVolumeDown")
    await wait_until(lambda: {"24", "25"} <= set(inputs.keys()), 15, what="volume keys at the window")
    await send("GoHome")
    await wait_until(lambda: "dev.emulatorhub.e2e" not in focused(), 10, what="home")
    await send("AppSwitch")
    await wait_until(lambda: re.search(r"Recents|recents|Launcher|Overview|tvlauncher", focused()), 10)
    await send("Power")
    await wait_until(lambda: "mWakefulness=Asleep" in awake(), 10, what="screen off")
    await send("Power")
    await wait_until(lambda: "mWakefulness=Awake" in awake(), 10, what="screen on")


@pytest.mark.android
async def test_text_input_with_symbols_unicode_and_truncation(env, inputs, leased):
    sample = "hi there! #$%&*()_+-=[] 你好 é"
    long = "x" * 600
    async with open_live(leased["lease_id"]) as ws:
        await ws.send(json.dumps({"t": "text", "text": sample}))
        await asyncio.sleep(1)
        await ws.send(json.dumps({"t": "text", "text": long}))
        await recv_frames(ws, 1)
    if env.real:
        # The hub types printable ASCII only (all the emulator can translate):
        # 你好 and é are skipped and the rest arrives.
        ascii_sample = "".join(c for c in sample if 32 <= ord(c) < 127)
        want = ascii_sample + "x" * 500

        def typed():
            # The probe logs the whole field on every change; logcat splits long
            # lines, so compare only the character counts and the prefix.
            t = inputs.text()
            return t if len(t) >= len(want) - 15 else None

        try:
            text = await wait_until(typed, 180, interval=3, what="all text typed")
        except AssertionError:
            raise AssertionError(f"typed so far ({len(inputs.text())} chars): {inputs.text()[:120]!r}") from None
        assert text.startswith(ascii_sample.rstrip()), text[:80]
        # Truncation: never more than 500 of the 600. (An exact 500 is asserted
        # on the fake; a 2-core CI guest can drop a few keystrokes of a 500-char
        # burst, and the image contract checks exact round-trips of real text.)
        assert 490 <= text.count("x") <= 500, text.count("x")
    else:
        # Exact on the fake: untypeable characters skipped, truncated at 500,
        # nothing lost (500 chars at 12/s is ~45s of paced typing).
        want = "".join(c for c in sample if 32 <= ord(c) < 127) + "x" * 500
        text = await wait_until(lambda: len(inputs.text()) >= len(want) and inputs.text(), 90, interval=2)
        assert text == want


@pytest.mark.xfail(
    strict=True,
    reason="handle_input raises on malformed client messages and the exception ends the session (ENG-339)",
)
@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        json.dumps({"t": "touch", "y": 0.5, "down": True}),
        json.dumps({"t": "touch", "x": "abc", "y": 0, "down": True}),
        b"\x00\x01binary",
    ],  # fmt: skip
    ids=["non-json", "missing-x", "string-x", "binary"],
)
async def test_malformed_input_does_not_kill_the_session(env, leased, bad):
    async with open_live(leased["lease_id"]) as ws:
        await recv_frames(ws, 1)
        await ws.send(bad)
        await asyncio.sleep(1)
        await ws.send(json.dumps({"t": "touch", "x": 0.5, "y": 0.5, "down": True}))
        frames = await recv_frames(ws, 3, timeout=10)
        assert frames


@pytest.mark.android
async def test_several_viewers_at_once(env, inputs, leased):
    async with (
        open_live(leased["lease_id"]) as a,
        open_live(leased["lease_id"]) as b,
        open_live(leased["lease_id"]) as c,
    ):
        for ws in (a, b, c):
            await recv_frames(ws, 1, timeout=60)
        for i, ws in enumerate((a, b, c)):
            # Distinct points, spaced out: a slow guest coalesces identical rapid taps.
            await ws.send(json.dumps({"t": "touch", "x": 0.3 + 0.1 * i, "y": 0.4, "down": True}))
            await ws.send(json.dumps({"t": "touch", "x": 0.3 + 0.1 * i, "y": 0.4, "down": False}))
            await asyncio.sleep(0.7)
        for ws in (a, b, c):
            assert await recv_frames(ws, 1)
        try:
            await wait_until(lambda: len([t for t in inputs.touches() if t[2]]) >= 3, 30, what="3 viewers' taps")
        except AssertionError:
            raise AssertionError(f"touches: {inputs.touches()}") from None
        await c.close()
        for i, ws in enumerate((a, b)):
            await ws.send(json.dumps({"t": "touch", "x": 0.6, "y": 0.5 + 0.1 * i, "down": True}))
            await ws.send(json.dumps({"t": "touch", "x": 0.6, "y": 0.5 + 0.1 * i, "down": False}))
            await asyncio.sleep(0.7)
            assert await recv_frames(ws, 1)
        # Input from the surviving viewers still lands (checked before closing them).
        await wait_until(lambda: len([t for t in inputs.touches() if t[2]]) >= 5, 20, what="taps after one left")


@pytest.mark.parametrize("ending", ["release", "expiry", "lost"])
async def test_lease_ending_closes_the_viewer_and_frees_the_channel(env, mcp, kube, hubctl, profile, holder, ending):
    grant = await acquire_leased(mcp, env, profile, holder, ttl_minutes=1 if ending == "expiry" else 30)
    pod = pod_name(grant["slot"], grant["lease_id"])
    fds_before = hubctl.fd_count()
    async with open_live(grant["lease_id"]) as ws:
        await recv_frames(ws, 2)
        if ending == "release":
            await mcp.call("release", lease_id=grant["lease_id"])
        elif ending == "lost":
            kube.delete_pod(pod, grace=0)
        started = time.monotonic()
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            while True:
                await asyncio.wait_for(ws.recv(), 120)
        if ending != "expiry":
            assert time.monotonic() - started < 30
    # One warning in the hub log for this lease, and no leaked channel.
    await wait_until(lambda: hubctl.fd_count() <= fds_before + 2, 30, what="fds back to baseline")
    assert hubctl.logs().count(f"live view for lease {grant['lease_id']} ended") <= 1


async def test_reconcile_rebuilds_the_live_view_after_a_hub_restart(env, mcp, hubctl, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    hubctl.restart()
    async with ui_client() as ui:
        r = await ui.get(f"/api/leases/{grant['lease_id']}/snapshot")
        assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    async with open_live(grant["lease_id"]) as ws:
        await ws.send(json.dumps({"t": "touch", "x": 0.5, "y": 0.5, "down": True}))
        assert await recv_frames(ws, 2)


@pytest.mark.fake_only
async def test_abandoned_viewer_is_cleaned_up_server_side(env, kube, inputs, leased):
    """A viewer that vanishes without a close frame (a killed tab) still frees
    its gRPC stream on the emulator."""
    import socket

    ws = await open_live(leased["lease_id"], close_timeout=0.1)
    await recv_frames(ws, 2)
    await wait_until(lambda: inputs.open_streams() == 1, 10)
    # Drop the TCP connection without a close handshake.
    ws.transport.get_extra_info("socket").shutdown(socket.SHUT_RDWR)
    ws.transport.abort()
    await wait_until(lambda: inputs.open_streams() == 0, 60, what="server-side stream to close")


@pytest.mark.android
async def test_wss_through_the_ingress(env, leased):
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = f"wss://{UI_HOST}/api/leases/{leased['lease_id']}/live"
    async with websockets.connect(url, additional_headers=COOKIE, ssl=ctx, max_size=None) as ws:
        await ws.send(json.dumps({"t": "touch", "x": 0.5, "y": 0.5, "down": True}))
        assert (await recv_frames(ws, 2))[0][:2] == b"\xff\xd8"


@pytest.mark.android
async def test_bandwidth_budget(env, leased):
    """One viewer for 60s stays under 2 MB/s (480px JPEG at quality 70)."""
    budget = 2 * 1024 * 1024
    total = 0
    async with open_live(leased["lease_id"]) as ws:
        await ws.send(json.dumps({"t": "touch", "x": 0.5, "y": 0.5, "down": True}))
        end = time.monotonic() + 60
        while time.monotonic() < end:
            try:
                total += len(await asyncio.wait_for(ws.recv(), max(0.1, end - time.monotonic())))
            except TimeoutError:
                break
    assert total / 60 < budget, f"{total / 60 / 1024:.0f} KB/s"


# ------------------------------------------------------------------ browser


@pytest.mark.android
async def test_browser_live_dialog_streams_and_sends_input(env, inputs, leased):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(base_url=f"http://{UI_HOST}")
        await ctx.add_cookies([{"name": "e2e_user", "value": "noah", "domain": UI_HOST, "path": "/"}])
        page = await ctx.new_page()
        closed: list[bool] = []
        page.on("websocket", lambda ws: ws.on("close", lambda _: closed.append(True)))
        await page.goto("/")
        card = page.locator("#slots .card").nth(leased["slot"])
        await card.get_by_role("button", name="Open live view").click()
        dialog = page.locator("#live-dialog")
        await expect(dialog).to_be_visible()
        canvas = page.locator("#live-canvas")
        # Drawn frames resize the canvas to the frame size (480 wide or less).
        await expect(canvas).to_have_attribute("width", re.compile(r"^(480|[1-4]\d\d)$"), timeout=20_000)
        box = await canvas.bounding_box()
        await page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
        await page.mouse.move(box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.6)
        await page.mouse.down()
        await page.mouse.move(box["x"] + box["width"] * 0.7, box["y"] + box["height"] * 0.6, steps=5)
        await page.mouse.up()
        await page.keyboard.type("ab")
        await page.keyboard.press("Enter")
        await dialog.get_by_role("button", name="Close").click()
        await expect(dialog).to_be_hidden()
        await wait_until(lambda: closed, 10, what="websocket close")
        await browser.close()
    w, h = inputs.size()
    downs = await wait_until(lambda: [t for t in inputs.touches() if t[2]], 20)
    x, y, _ = downs[0]
    assert abs(x - w / 2) <= 0.05 * w and abs(y - h / 2) <= 0.05 * h
    if env.real:
        await wait_until(lambda: "ab" in inputs.text(), 20)
    else:
        assert "ab" in inputs.text()
        assert "Enter" in inputs.keys()
        await wait_until(lambda: inputs.open_streams() == 0, 30, what="stream closed after dialog close")
