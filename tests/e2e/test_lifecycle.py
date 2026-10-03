"""ENG-335: one lease at a time, through every state and every end reason,
against real Pods (and, with E2E_EMULATOR=real, real Android devices)."""

import asyncio
import subprocess
import time

import pytest

from emulator_hub.catalog import DEVICES, SYSTEM_IMAGES
from tests.e2e.hub import (
    APK,
    McpError,
    acquire_leased,
    pod_name,
    tcp_banner,
    ui_client,
    wait_until,
)

# Every device in the catalog, each with every system image that can boot it.
MATRIX = [
    (ff, image, device)
    for ff, devices in DEVICES.items()
    for device in devices
    for image, spec in SYSTEM_IMAGES.items()
    if ff in spec.form_factors
]
# Physical display size per catalog device (avdmanager hardware definitions).
DISPLAY = {
    "pixel_8": (1080, 2400),
    "medium_phone": (1080, 2400),
    "pixel_tablet": (2560, 1600),
    "medium_tablet": (2560, 1600),
    "tv_1080p": (1920, 1080),
    "tv_720p": (1280, 720),
}


@pytest.fixture
async def matrix_profile(request, env):
    ff, image, device = request.param
    name = f"m-{device.replace('_', '-')}"
    body = {
        "form_factor": ff,
        "system_image": image,
        "device": device,
        "ram_mb": 2048 if env.real else 1024,
        "cores": 2 if env.real else 1,
    }
    async with ui_client("e2e-setup") as ui:
        assert (await ui.put(f"/api/profiles/{name}", json=body)).status_code == 200
    yield name, body
    async with ui_client("e2e-setup") as ui:
        await ui.delete(f"/api/profiles/{name}")


# ------------------------------------------------------------------ happy path


@pytest.mark.android
@pytest.mark.parametrize("matrix_profile", MATRIX, indirect=True, ids=[m[2] for m in MATRIX])
async def test_every_catalog_device_boots_and_matches_its_profile(mcp, env, kube, adb, matrix_profile, holder):
    name, body = matrix_profile
    before = time.time()
    grant = await mcp.call("acquire", profile=name, holder=holder, ttl_minutes=30, boot_wait_seconds=600)
    assert grant["state"] == "leased", grant
    slot = grant["slot"]
    assert grant["adb"] == f"{env.slot_ips[slot]}:5555"
    assert grant["in_cluster"] == f"slot-{slot}.emulator-hub.svc.cluster.local:5555"
    assert before + 30 * 60 - 5 <= grant["expires_at"] <= time.time() + 30 * 60 + 5

    pod = kube.pod(pod_name(slot, grant["lease_id"]))
    assert pod is not None
    labels = pod["metadata"]["labels"]
    assert labels["emulator-hub/slot"] == str(slot) and labels["emulator-hub/lease"] == grant["lease_id"]
    envs = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert envs == {
        "SYSTEM_IMAGE": SYSTEM_IMAGES[body["system_image"]].package,
        "DEVICE": body["device"],
        "RAM_MB": str(body["ram_mb"]),
        "CORES": str(body["cores"]),
    }

    if not env.real:
        # The fake answers on the adb port with its Pod name: the slot Service
        # routes to the right Pod.
        assert tcp_banner(env.slot_ips[slot], 5555) == pod["metadata"]["name"]
        return

    target = grant["adb"]
    if body["form_factor"] == "tv":
        try:
            adb.connect(target, timeout=60)
        except AssertionError as exc:
            if "unauthorized" in str(exc):
                pytest.xfail("android-tv rejects a never-seen adb key as unauthorized (ENG-342)")
            raise
    else:
        adb.connect(target)
    adb.wait_boot_completed(target)
    api = SYSTEM_IMAGES[body["system_image"]].api_level
    assert adb.shell(target, "getprop ro.build.version.sdk").strip() == str(api)
    w, h = DISPLAY[body["device"]]
    size = adb.shell(target, "wm size")
    assert f"{w}x{h}" in size or f"{h}x{w}" in size, size
    features = adb.shell(target, "pm list features")
    assert ("feature:android.software.leanback" in features) == (body["form_factor"] == "tv")
    assert int(adb.shell(target, "nproc").strip()) == body["cores"]
    mem_kb = int(adb.shell(target, "grep MemTotal /proc/meminfo").split()[1])
    # The emulator raises guest RAM to the system image's minimum (API 35: 2560
    # MB) and to the device definition's own RAM (pixel/medium tablets: 4 GB).
    from emulator_hub.catalog import DEVICE_MIN_RAM_MB

    effective = max(body["ram_mb"], SYSTEM_IMAGES[body["system_image"]].min_ram_mb,
                    DEVICE_MIN_RAM_MB.get(body["device"], 0))  # fmt: skip
    assert 0.6 * body["ram_mb"] * 1024 <= mem_kb <= 1.05 * effective * 1024, mem_kb
    # Usable: install, launch and screenshot the probe app.
    adb.install(target, APK)
    adb.focus_app(target, "dev.emulatorhub.e2e/.ProbeActivity")
    png = subprocess.run(
        ["adb", "-s", target, "exec-out", "screencap", "-p"], env=adb.env, capture_output=True, timeout=60
    ).stdout
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 1000
    await mcp.call("release", lease_id=grant["lease_id"])


@pytest.mark.android
async def test_in_cluster_endpoint_reaches_the_device(mcp, env, kube, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    host = grant["in_cluster"].rsplit(":", 1)[0]
    if env.real:
        out = kube.exec(
            "toolbox",
            "sh",
            "-c",
            f"adb connect {grant['in_cluster']} >/dev/null; sleep 2; "
            f"adb -s {grant['in_cluster']} shell getprop sys.boot_completed",
            ns="e2e-client",
            timeout=120,
        )
        assert out.strip().endswith("1"), out
    else:
        out = kube.exec("toolbox", "sh", "-c", f"nc -w 5 {host} 5555", ns="e2e-client")
        assert out.strip() == pod_name(grant["slot"], grant["lease_id"])


# ------------------------------------------------------------------ booting grants


@pytest.mark.android
async def test_booting_grant_then_poll_until_leased(mcp, env, kube, profile, holder):
    grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    assert grant["state"] == "booting" and grant["expires_at"] is None
    assert grant["adb"] == f"{env.slot_ips[grant['slot']]}:5555"
    st = await mcp.call("status")
    assert st["slots"][grant["slot"]]["state"] == "booting"
    assert st["slots"][grant["slot"]]["lease_id"] == grant["lease_id"]
    # Heartbeat while booting only reports; it extends nothing.
    hb = await mcp.call("heartbeat", lease_id=grant["lease_id"])
    if hb["state"] == "booting":
        assert hb["expires_at"] is None
    leased = await wait_until(lambda: _leased(mcp, grant["lease_id"]), env.boot_budget_s, interval=2, what="leased")
    assert leased["expires_at"] is not None
    st = await mcp.call("status")
    assert st["slots"][grant["slot"]]["state"] == "leased"


async def _leased(mcp, lease_id):
    hb = await mcp.call("heartbeat", lease_id=lease_id)
    return hb if hb["state"] == "leased" else None


async def test_background_boot_failure_is_reported_on_the_next_heartbeat(mcp, env, hub_env, profile, holder):
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:never-boots", HUB_BOOT_TIMEOUT_S="10")
    grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    assert grant["state"] == "booting"

    async def failed():
        try:
            await mcp.call("heartbeat", lease_id=grant["lease_id"])
        except McpError as exc:
            return str(exc)

    msg = await wait_until(failed, 60, interval=1, what="boot to fail")
    assert "is ended (boot_failed); acquire a new one" in msg


# ------------------------------------------------------------------ heartbeat, release, expiry


async def test_heartbeat_keeps_a_one_minute_lease_alive(mcp, env, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder, ttl_minutes=1)
    first = grant["expires_at"]
    assert first - time.time() <= 61
    end = time.monotonic() + 150
    while time.monotonic() < end:
        await asyncio.sleep(20)
        hb = await mcp.call("heartbeat", lease_id=grant["lease_id"])
        assert hb["state"] == "leased"
        assert abs(hb["expires_at"] - (time.time() + 60)) < 5
    assert hb["expires_at"] > first + 100


async def test_max_age_is_a_hard_stop_despite_heartbeats(mcp, env, hub_env, profile, holder):
    hub_env(HUB_MAX_AGE_S="40")
    grant = await acquire_leased(mcp, env, profile, holder, ttl_minutes=5)
    lease_id = grant["lease_id"]
    hb = await mcp.call("heartbeat", lease_id=lease_id)
    # Never extended past created_at + max_age.
    async with ui_client() as ui:
        created = next(row for row in (await ui.get("/api/leases")).json() if row["id"] == lease_id)["created_at"]
    assert hb["expires_at"] <= created + 40 + 1

    async def ended():
        try:
            await mcp.call("heartbeat", lease_id=lease_id)
        except McpError as exc:
            return str(exc)

    msg = await wait_until(ended, 90, interval=2, what="max_age")
    assert "(max_age)" in msg or "(expired)" in msg
    async with ui_client() as ui:
        row = next(r for r in (await ui.get("/api/leases")).json() if r["id"] == lease_id)
    # The reaper checks max_age first; expires_at was capped at the same instant.
    assert row["end_reason"] in ("max_age", "expired")


@pytest.mark.android
async def test_release_deletes_the_pod_and_frees_the_slot(mcp, env, kube, adb, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    name = pod_name(grant["slot"], grant["lease_id"])
    if env.real:
        adb.connect(grant["adb"])
    released = await mcp.call("release", lease_id=grant["lease_id"])
    assert released == {"lease_id": grant["lease_id"], "state": "ended", "end_reason": "released"}
    await wait_until(lambda: kube.pod(name) is None, 20, what="Pod deletion")
    st = await mcp.call("status")
    assert st["slots"][grant["slot"]] == {
        "slot": grant["slot"],
        "state": "free",
        "lease_id": None,
        "profile": None,
        "holder": None,
        "expires_at": None,
    }
    async with ui_client() as ui:
        row = next(r for r in (await ui.get("/api/leases")).json() if r["id"] == grant["lease_id"])
    assert row["end_reason"] == "released" and row["ended_at"] is not None
    if env.real:
        await wait_until(
            lambda: "device" not in adb.run("-s", grant["adb"], "get-state", check=False), 60, what="adb to drop"
        )


@pytest.mark.real_emulator
async def test_every_lease_gets_a_fresh_device(mcp, env, adb, profile, holder):
    a = await acquire_leased(mcp, env, profile, holder)
    adb.connect(a["adb"])
    adb.install(a["adb"], APK)
    adb.shell(a["adb"], "echo marker > /sdcard/e2e-marker")
    await mcp.call("release", lease_id=a["lease_id"])
    b = await acquire_leased(mcp, env, profile, holder)
    assert b["slot"] == a["slot"]  # lowest free slot again
    adb.disconnect(b["adb"])
    adb.connect(b["adb"])
    adb.wait_boot_completed(b["adb"])
    assert "dev.emulatorhub.e2e" not in adb.shell(b["adb"], "pm list packages")
    assert "No such file" in adb.run("-s", b["adb"], "shell", "cat /sdcard/e2e-marker", check=False) or (
        adb.shell(b["adb"], "ls /sdcard/e2e-marker 2>/dev/null || true").strip() == ""
    )


async def test_unheartbeated_lease_expires(mcp, env, kube, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder, ttl_minutes=1)
    name = pod_name(grant["slot"], grant["lease_id"])
    remaining = grant["expires_at"] - time.time()

    async def gone():
        st = await mcp.call("status")
        return st["slots"][grant["slot"]]["lease_id"] != grant["lease_id"]

    await wait_until(gone, remaining + 30, interval=1, what="expiry")
    await wait_until(lambda: kube.pod(name) is None, 30, what="Pod deletion")
    for tool in ("heartbeat", "release"):
        with pytest.raises(McpError, match=r"\(expired\)"):
            await mcp.call(tool, lease_id=grant["lease_id"])


async def test_release_while_booting_leaves_no_pod(mcp, env, kube, hub_env, profile, holder):
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:slow")
    grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    assert grant["state"] == "booting"
    name = pod_name(grant["slot"], grant["lease_id"])
    await wait_until(lambda: kube.pod(name), 30, what="Pod create")
    released = await mcp.call("release", lease_id=grant["lease_id"])
    assert released["end_reason"] == "released"
    await wait_until(lambda: kube.pod(name) is None, 30, what="Pod deletion")
    # Still gone (and never activated) after the boot would have finished.
    await asyncio.sleep(10)
    assert kube.pod(name) is None
    async with ui_client() as ui:
        row = next(r for r in (await ui.get("/api/leases")).json() if r["id"] == grant["lease_id"])
    assert row["state"] == "ended" and row["end_reason"] == "released" and row["expires_at"] is None


async def test_release_racing_pod_creation_leaves_no_pod(mcp, env, kube, profile, holder):
    """Release immediately after acquire returns, before the Pod even exists."""
    for _ in range(3):
        grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
        await mcp.call("release", lease_id=grant["lease_id"])
        name = pod_name(grant["slot"], grant["lease_id"])
        await asyncio.sleep(3)
        assert kube.pod(name) is None or kube.pod(name)["metadata"].get("deletionTimestamp")


# ------------------------------------------------------------------ lost and boot failures


async def test_deleted_pod_is_reaped_as_lost(mcp, env, kube, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    kube.delete_pod(pod_name(grant["slot"], grant["lease_id"]), grace=0)
    await _assert_ended(mcp, grant, "lost")


async def _assert_ended(mcp, grant, reason, timeout=60):
    async def ended():
        try:
            await mcp.call("heartbeat", lease_id=grant["lease_id"])
        except McpError as exc:
            return str(exc)

    msg = await wait_until(ended, timeout, interval=1, what=reason)
    assert f"({reason})" in msg, msg
    st = await mcp.call("status")
    assert st["slots"][grant["slot"]]["lease_id"] != grant["lease_id"]


@pytest.mark.android
async def test_killed_emulator_process_is_reaped_as_lost(mcp, env, kube, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    name = pod_name(grant["slot"], grant["lease_id"])
    # PID 1 is the emulator (exec'd by entrypoint.sh) or the fake; SIGKILL it.
    kube.kill_container(name)
    await _assert_ended(mcp, grant, "lost", timeout=90)
    await wait_until(lambda: kube.pod(name) is None, 30, what="Pod deletion")


async def test_boot_timeout_is_boot_failed(mcp, env, kube, hub_env, profile, holder):
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:never-boots", HUB_BOOT_TIMEOUT_S="8")
    with pytest.raises(McpError, match=r"did not finish booting within 8s"):
        await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=120)
    assert not kube.emulator_pods() or await wait_until(lambda: not kube.pod_names(), 30)
    assert (await mcp.call("status"))["slots"][0]["state"] == "free"


async def test_pod_exiting_during_boot_is_boot_failed(mcp, env, hub_env, profile, holder):
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:exit-during-boot")
    with pytest.raises(McpError, match=r"exited during boot"):
        await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=120)


async def test_unschedulable_pod_is_boot_failed_and_cleaned_up(mcp, env, kube, hub_env, profile, holder):
    hub_env(HUB_BOOT_TIMEOUT_S="15")
    kube.run("cordon", env.kvm_node)
    try:
        grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
        name = pod_name(grant["slot"], grant["lease_id"])
        pod = await wait_until(lambda: kube.pod(name), 30, what="Pod create")
        assert pod["status"]["phase"] == "Pending"
        await _assert_ended(mcp, grant, "boot_failed", timeout=60)
        await wait_until(lambda: kube.pod(name) is None, 30, what="Pending Pod deletion")
    finally:
        kube.run("uncordon", env.kvm_node)


async def test_unpullable_image_is_boot_failed(mcp, env, kube, hub_env, profile, holder):
    hub_env(HUB_EMULATOR_IMAGE="registry.invalid/emulator-hub/does-not-exist:nope", HUB_BOOT_TIMEOUT_S="20")
    grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    await _assert_ended(mcp, grant, "boot_failed", timeout=90)
    await wait_until(lambda: kube.pod(pod_name(grant["slot"], grant["lease_id"])) is None, 30)


async def test_api_rejecting_pod_create_is_boot_failed_immediately(mcp, env, kube, profile, holder):
    kube.apply(
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "e2e-no-pods", "namespace": "emulator-hub"},
            "spec": {"hard": {"count/pods": "0"}},
        }
    )
    try:
        # A new quota only bites once the quota controller has computed its usage.
        await wait_until(
            lambda: (
                kube.json("-n", "emulator-hub", "get", "resourcequota", "e2e-no-pods").get("status", {}).get("used")
            ),
            30,
            what="quota status",
        )
        started = time.monotonic()
        with pytest.raises(McpError) as err:
            await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=60)
        assert time.monotonic() - started < 20
        assert "could not start the emulator" in str(err.value) and "forbidden" in str(err.value).lower()
        assert all(s["state"] == "free" for s in (await mcp.call("status"))["slots"])
    finally:
        kube.run("-n", "emulator-hub", "delete", "resourcequota", "e2e-no-pods", "--ignore-not-found")


# ------------------------------------------------------------------ validation


async def test_validation_errors_reach_mcp_and_rest(mcp, env, profile):
    cases = [
        (dict(profile="nope", holder="x"), r"no profile named 'nope'", 404),
        (dict(profile=profile, holder="x", ttl_minutes=0), r"between 1 and 120", 422),
        (dict(profile=profile, holder="x", ttl_minutes=121), r"between 1 and 120", 422),
        (dict(profile=profile, holder="   "), r"holder must say who you are", None),
    ]
    async with ui_client() as ui:
        for args, msg, code in cases:
            with pytest.raises(McpError, match=msg):
                await mcp.call("acquire", **args)
            if code:
                body = {"profile": args["profile"], "ttl_minutes": args.get("ttl_minutes", 30)}
                r = await ui.post("/api/leases", json=body)
                assert r.status_code == code, r.text
        for tool in ("heartbeat", "release"):
            with pytest.raises(McpError, match=r"no lease 'nope'"):
                await mcp.call(tool, lease_id="nope")
            r = await (ui.post("/api/leases/nope/heartbeat") if tool == "heartbeat" else ui.delete("/api/leases/nope"))
            assert r.status_code == 404


async def test_long_holder_is_truncated_and_double_release_is_refused(mcp, env, profile):
    grant = await mcp.call("acquire", profile=profile, holder="h" * 200, boot_wait_seconds=0)
    st = await mcp.call("status")
    assert st["slots"][grant["slot"]]["holder"] == "h" * 120
    await mcp.call("release", lease_id=grant["lease_id"])
    with pytest.raises(McpError, match=r"already ended \(released\)"):
        await mcp.call("release", lease_id=grant["lease_id"])
