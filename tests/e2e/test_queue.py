"""ENG-336: the FIFO queue and the single-event-loop locking model under real
network clients and real boot latency."""

import asyncio
import time

import httpx
import pytest

from tests.e2e.hub import (
    McpError,
    McpHolder,
    acquire_leased,
    metric,
    metrics_text,
    pod_name,
    ui_client,
    wait_until,
)


async def fill_all_slots(mcp, env, profile, holder) -> list[dict]:
    grants = await asyncio.gather(
        *(acquire_leased(mcp, env, profile, f"{holder}-fill{i}") for i in range(len(env.slot_ips)))
    )
    assert sorted(g["slot"] for g in grants) == list(range(len(env.slot_ips)))
    return list(grants)


async def queue_depth(mcp) -> int:
    return (await mcp.call("status"))["queue_depth"]


async def test_full_pool_is_busy_with_a_position(mcp, env, profile, holder):
    await fill_all_slots(mcp, env, profile, holder)
    with pytest.raises(McpError, match=r"no emulator slot free; you were number 1 in the queue"):
        await mcp.call("acquire", profile=profile, holder=holder, wait_seconds=0)
    async with ui_client() as ui:
        r = await ui.post("/api/leases", json={"profile": profile, "wait_seconds": 0})
    assert r.status_code == 409
    assert r.json()["detail"] == {"message": "no emulator slot free; you were number 1 in the queue", "position": 1}


async def test_waiters_are_served_in_arrival_order(env, mcp, profile, holder):
    fills = await fill_all_slots(mcp, env, profile, holder)
    order: list[str] = []
    sessions = [McpHolder(env) for _ in range(3)]
    clients = [await s.__aenter__() for s in sessions]

    async def waiter(m, name):
        g = await m.call("acquire", profile=profile, holder=name, wait_seconds=600, boot_wait_seconds=0)
        order.append(name)
        return g

    try:
        tasks = []
        for i, m in enumerate(clients):
            tasks.append(asyncio.create_task(waiter(m, f"{holder}-W{i}")))
            await wait_until(lambda i=i: _depth_is(mcp, i + 1), 30, what=f"W{i} queued")
        # Free one slot at a time: release a fill, or (once fills run out) the
        # lease the earliest waiter got.
        to_free = list(fills)
        for i, task in enumerate(tasks):
            freed = to_free.pop(0)
            await mcp.call("release", lease_id=freed["lease_id"])
            got = await asyncio.wait_for(task, 60)
            assert got["slot"] == freed["slot"], "the freed slot goes to the oldest waiter"
            assert all(not t.done() for t in tasks[i + 1 :]), "only one waiter per freed slot"
            to_free.append(got)
        assert order == [f"{holder}-W{i}" for i in range(3)]
    finally:
        for s_ in sessions:
            await s_.__aexit__()


async def _depth_is(mcp, n):
    return await queue_depth(mcp) == n


async def test_no_queue_jumping(env, mcp, profile, holder):
    fills = await fill_all_slots(mcp, env, profile, holder)
    async with McpHolder(env) as m1:
        w1 = asyncio.create_task(m1.call("acquire", profile=profile, holder=f"{holder}-W1", wait_seconds=600))
        await wait_until(lambda: _depth_is(mcp, 1), 30)
        await mcp.call("release", lease_id=fills[0]["lease_id"])
        # Same instant: a newcomer that will not wait must not take W1's slot.
        with pytest.raises(McpError, match="no emulator slot free"):
            await mcp.call("acquire", profile=profile, holder=f"{holder}-jumper", wait_seconds=0)
        got = await asyncio.wait_for(w1, env.boot_budget_s)
        assert got["slot"] == fills[0]["slot"]


async def test_waiter_timeout_leaves_the_queue(env, mcp, profile, holder):
    fills = await fill_all_slots(mcp, env, profile, holder)
    started = time.monotonic()
    with pytest.raises(McpError, match="number 1 in the queue"):
        await mcp.call("acquire", profile=profile, holder=f"{holder}-impatient", wait_seconds=5)
    assert 4.5 <= time.monotonic() - started < 20
    assert await queue_depth(mcp) == 0
    # The next release frees the slot instead of handing it to the dead waiter.
    await mcp.call("release", lease_id=fills[0]["lease_id"])
    st = await mcp.call("status")
    assert st["slots"][fills[0]["slot"]]["state"] == "free"


async def test_mcp_wait_seconds_is_clamped(env, mcp, profile, holder):
    fills = await fill_all_slots(mcp, env, profile, holder)
    # Negative clamps to 0: an immediate Busy, not a validation error.
    started = time.monotonic()
    with pytest.raises(McpError, match="no emulator slot free"):
        await mcp.call("acquire", profile=profile, holder=holder, wait_seconds=-50)
    assert time.monotonic() - started < 5
    # Huge clamps to 600: the caller is queued (not rejected); cancel it after a bit.
    # Huge clamps to 600 (not rejected, not unbounded): the caller is queued.
    async with McpHolder(env) as m:
        task = asyncio.create_task(m.call("acquire", profile=profile, holder=holder, wait_seconds=10**9))
        await wait_until(lambda: _depth_is(mcp, 1), 30)
        await asyncio.sleep(2)
        assert not task.done()
        # Serve it rather than abandon it (an abandoned waiter lingers, ENG-336).
        await mcp.call("release", lease_id=fills[0]["lease_id"])
        got = await asyncio.wait_for(task, 60)
        assert got["slot"] == fills[0]["slot"]


@pytest.mark.parametrize("how", ["release", "expiry", "lost", "boot_failure", "cancelled_boot"])
async def test_every_way_a_slot_frees_hands_it_to_the_oldest_waiter(env, mcp, kube, hub_env, profile, holder, how):
    """Fill the pool so that slot 0's lease ends by `how`, queue a waiter, and
    check the waiter gets slot 0."""
    if how == "boot_failure":
        hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:never-boots", HUB_BOOT_TIMEOUT_S="15")
    elif how == "cancelled_boot":
        hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:slow")
    # Slot 0's victim first, so it lands on slot 0.
    if how in ("boot_failure", "cancelled_boot"):
        victim = await mcp.call("acquire", profile=profile, holder=f"{holder}-victim", boot_wait_seconds=0)
        assert victim["state"] == "booting"
    else:
        victim = await acquire_leased(mcp, env, profile, f"{holder}-victim", ttl_minutes=1 if how == "expiry" else 30)
    assert victim["slot"] == 0
    # The rest of the pool is held by leases that do not end during the test.
    for i in range(1, len(env.slot_ips)):
        g = await mcp.call("acquire", profile=profile, holder=f"{holder}-fill{i}", boot_wait_seconds=0)
        assert g["slot"] == i
    async with McpHolder(env) as m:
        waiter = asyncio.create_task(
            m.call("acquire", profile=profile, holder=f"{holder}-waiter", wait_seconds=600, boot_wait_seconds=0)
        )
        await wait_until(lambda: _depth_is(mcp, 1), 30, what="waiter queued")
        if how in ("release", "cancelled_boot"):
            await mcp.call("release", lease_id=victim["lease_id"])
        elif how == "lost":
            kube.delete_pod(pod_name(victim["slot"], victim["lease_id"]), grace=0)
        # expiry and boot_failure: the hub ends the lease on its own.
        got = await asyncio.wait_for(waiter, 180)
    assert got["slot"] == victim["slot"] and got["lease_id"] != victim["lease_id"]
    async with ui_client() as ui:
        row = next(r for r in (await ui.get("/api/leases")).json() if r["id"] == victim["lease_id"])
    expected = {
        "release": "released",
        "expiry": "expired",
        "lost": "lost",
        "boot_failure": "boot_failed",
        "cancelled_boot": "released",
    }[how]
    assert row["end_reason"] == expected


@pytest.mark.xfail(
    strict=True,
    reason="the hub never notices an MCP caller that disconnects while queued: the waiter keeps its place "
    "and is later granted (and boots) a slot nobody will use (ENG-336)",
)
async def test_client_disconnect_while_queued_removes_the_waiter(env, mcp, profile, holder):
    fills = await fill_all_slots(mcp, env, profile, holder)
    async with McpHolder(env) as m:
        task = asyncio.create_task(m.call("acquire", profile=profile, holder=f"{holder}-gone", wait_seconds=60))
        await wait_until(lambda: _depth_is(mcp, 1), 30)
    # Leaving the session closed the HTTP request mid-wait.
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await wait_until(lambda: _depth_is(mcp, 0), 30, what="dead waiter to leave the queue")
    await mcp.call("release", lease_id=fills[0]["lease_id"])
    st = await mcp.call("status")
    assert st["slots"][fills[0]["slot"]] == {
        "slot": fills[0]["slot"],
        "state": "free",
        "lease_id": None,
        "profile": None,
        "holder": None,
        "expires_at": None,
    }


@pytest.mark.xfail(
    strict=True,
    reason="a REST client that disconnects mid-boot is not noticed: the boot runs on and the lease is "
    "activated for nobody instead of ending as cancelled (ENG-336)",
)
async def test_rest_client_disconnect_during_foreground_boot_cancels_the_lease(env, mcp, kube, hub_env, profile):
    # Boot (75s) inside the boot timeout, so the only way to end is the disconnect.
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:slow", HUB_BOOT_TIMEOUT_S="150")
    async with ui_client("disconnecter", timeout=httpx.Timeout(8, connect=5)) as ui:
        with pytest.raises(httpx.ReadTimeout):
            await ui.post("/api/leases", json={"profile": profile})
    async with ui_client() as ui:

        async def settled():
            rows = (await ui.get("/api/leases")).json()
            row = next((r for r in rows if r["holder"] == "ui:disconnecter"), None)
            return row if row and row["state"] != "booting" else None

        row = await wait_until(settled, 150, interval=2, what="the abandoned boot to settle")
    assert row["end_reason"] == "cancelled"
    await wait_until(lambda: not kube.pod_names(), 30, what="Pod deletion")


async def test_mcp_boot_survives_client_disconnect_after_a_booting_grant(env, mcp, profile, holder):
    async with McpHolder(env) as m:
        grant = await m.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    assert grant["state"] == "booting"
    # The client is gone; the boot carries on and the lease is recoverable.
    st = await wait_until(
        lambda: _slot_state(mcp, grant["slot"], "leased"), env.boot_budget_s, interval=2, what="background boot"
    )
    assert st["lease_id"] == grant["lease_id"] and st["holder"] == holder
    hb = await mcp.call("heartbeat", lease_id=grant["lease_id"])
    assert hb["state"] == "leased"


async def _slot_state(mcp, slot, state):
    s = (await mcp.call("status"))["slots"][slot]
    return s if s["state"] == state else None


async def test_burst_without_waiting_never_shares_a_slot(env, mcp, kube, profile, holder):
    n = len(env.slot_ips)
    results = await asyncio.gather(
        *(
            mcp.call("acquire", profile=profile, holder=f"{holder}-{i}", wait_seconds=0, boot_wait_seconds=0)
            for i in range(10)
        ),
        return_exceptions=True,
    )
    ok = [r for r in results if isinstance(r, dict)]
    busy = [r for r in results if isinstance(r, McpError)]
    assert len(ok) == n and len(busy) == 10 - n, results
    assert sorted(g["slot"] for g in ok) == list(range(n))
    await wait_until(lambda: len(kube.pod_names()) == n, 60)
    assert kube.pod_names() == {pod_name(g["slot"], g["lease_id"]) for g in ok}


async def test_burst_with_waiting_eventually_serves_everyone_in_order(env, mcp, profile, holder):
    served: list[int] = []
    sessions = [McpHolder(env) for _ in range(6)]
    clients = [await s_.__aenter__() for s_ in sessions]

    async def worker(i, m):
        g = await m.call("acquire", profile=profile, holder=f"{holder}-{i}", wait_seconds=600, boot_wait_seconds=0)
        g = await _until_leased(m, env, g)
        served.append(i)
        await m.call("release", lease_id=g["lease_id"])

    try:
        tasks = []
        for i, m in enumerate(clients):
            tasks.append(asyncio.create_task(worker(i, m)))
            await asyncio.sleep(0.3)  # distinct arrival order
        await asyncio.wait_for(asyncio.gather(*tasks), 6 * env.boot_budget_s)
    finally:
        for s_ in sessions:
            await s_.__aexit__()
    n = len(env.slot_ips)
    assert sorted(served) == list(range(6))
    # Everyone who had to queue was served in arrival order.
    assert served[n:] == sorted(served[n:]) and min(served[n:]) >= n


async def _until_leased(m, env, g):
    async def poll():
        nonlocal g
        if g["state"] != "leased":
            g = await m.call("heartbeat", lease_id=g["lease_id"])
        return g if g["state"] == "leased" else None

    return await wait_until(poll, env.boot_budget_s, interval=1)


async def test_concurrent_release_heartbeat_and_expiry_end_a_lease_once(env, mcp, kube, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder, ttl_minutes=1)
    # Line up release + heartbeats right as the reaper is due to expire it.
    await asyncio.sleep(max(0, grant["expires_at"] - time.time() - 0.5))
    results = await asyncio.gather(
        mcp.call("release", lease_id=grant["lease_id"]),
        mcp.call("release", lease_id=grant["lease_id"]),
        mcp.call("heartbeat", lease_id=grant["lease_id"]),
        return_exceptions=True,
    )
    releases = [r for r in results[:2] if isinstance(r, dict)]
    assert len(releases) <= 1
    async with ui_client() as ui:
        rows = [r for r in (await ui.get("/api/leases")).json() if r["id"] == grant["lease_id"]]
    assert len(rows) == 1 and rows[0]["state"] == "ended"
    assert rows[0]["end_reason"] in ("released", "expired")
    st = await mcp.call("status")
    assert st["slots"][grant["slot"]]["state"] == "free"


async def test_queue_depth_agrees_everywhere(env, mcp, profile, holder):
    fills = await fill_all_slots(mcp, env, profile, holder)
    sessions = [McpHolder(env) for _ in range(2)]
    clients = [await s.__aenter__() for s in sessions]
    tasks = [
        asyncio.create_task(
            c.call("acquire", profile=profile, holder=f"{holder}-q{i}", wait_seconds=60, boot_wait_seconds=0)
        )
        for i, c in enumerate(clients)
    ]
    try:
        await wait_until(lambda: _depth_is(mcp, 2), 30)
        assert (await mcp.call("list_profiles"))["queue_depth"] == 2
        assert (await mcp.call("list_profiles"))["free_slots"] == 0
        async with ui_client() as ui:
            assert (await ui.get("/api/status")).json()["queue_depth"] == 2
        assert metric(metrics_text(), "emulator_hub_queue_depth") == 2
    finally:
        for f in fills[:2]:
            await mcp.call("release", lease_id=f["lease_id"])
        await asyncio.gather(*tasks, return_exceptions=True)
        for s in sessions:
            await s.__aexit__()


async def test_soak_thirty_cycles_leak_nothing(env, mcp, kube, hubctl, adb, profile, holder):
    fds_before = hubctl.fd_count()
    rss_before = hubctl.rss_kb()
    for i in range(30):
        g = await acquire_leased(mcp, env, profile, f"{holder}-{i}")
        if env.real and i % 10 == 0:
            adb.connect(g["adb"])
            assert adb.shell(g["adb"], "getprop sys.boot_completed").strip() == "1"
            adb.disconnect(g["adb"])
        await mcp.call("release", lease_id=g["lease_id"])
    await wait_until(lambda: not kube.pod_names(), 60)
    fds_after = hubctl.fd_count()
    rss_after = hubctl.rss_kb()
    assert fds_after <= fds_before + 10, (fds_before, fds_after)
    assert rss_after <= rss_before * 1.5 + 20_000, (rss_before, rss_after)
