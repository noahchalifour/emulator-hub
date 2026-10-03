"""ENG-340: reconcile() and reaper resilience with the real hub process killed
(grace 0) and restarted inside the cluster."""

import asyncio
import json
import time

import pytest

from tests.e2e.hub import (
    NS,
    McpError,
    McpHolder,
    acquire_leased,
    pod_name,
    ui_client,
    wait_until,
)

pytestmark = pytest.mark.disruptive


async def lease_row(lease_id):
    async with ui_client() as ui:
        return next(r for r in (await ui.get("/api/leases?limit=500")).json() if r["id"] == lease_id)


@pytest.mark.android
async def test_active_lease_survives_a_hub_kill(env, mcp, kube, hubctl, adb, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    name = pod_name(grant["slot"], grant["lease_id"])
    if env.real:
        adb.connect(grant["adb"])
        uptime_before = float(adb.shell(grant["adb"], "cut -d' ' -f1 /proc/uptime"))
    pod_uid = kube.pod(name)["metadata"]["uid"]
    hubctl.kill()
    row = await lease_row(grant["lease_id"])
    assert row["state"] == "leased" and row["slot"] == grant["slot"] and row["expires_at"] == grant["expires_at"]
    assert kube.pod(name)["metadata"]["uid"] == pod_uid, "the emulator Pod was not recreated"
    if env.real:
        assert float(adb.shell(grant["adb"], "cut -d' ' -f1 /proc/uptime")) > uptime_before, "device did not reboot"
    async with McpHolder(env) as m:
        hb = await m.call("heartbeat", lease_id=grant["lease_id"])
        assert hb["state"] == "leased" and hb["expires_at"] >= grant["expires_at"]
        async with ui_client() as ui:
            r = await ui.get(f"/api/leases/{grant['lease_id']}/snapshot")
            assert r.status_code == 200
        await m.call("release", lease_id=grant["lease_id"])
    await wait_until(lambda: kube.pod(name) is None, 30)


async def test_booting_lease_is_lost_on_restart(env, mcp, kube, hubctl, hub_env, profile, holder):
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:slow")
    grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    name = pod_name(grant["slot"], grant["lease_id"])
    await wait_until(lambda: kube.pod(name), 30, what="Pod create")
    hubctl.kill()
    row = await lease_row(grant["lease_id"])
    assert row["state"] == "ended" and row["end_reason"] == "lost"
    await wait_until(lambda: kube.pod(name) is None, 30, what="orphan deletion")
    async with ui_client() as ui:
        assert (await ui.get("/api/status")).json()["slots"][grant["slot"]]["state"] == "free"


async def test_graceful_shutdown_cancels_a_booting_lease(env, mcp, kube, hubctl, hub_env, profile, holder):
    """SIGTERM (rollout, drain) cancels in-flight boots: the lease ends as
    cancelled and its Pod is cleaned up, never left half-booted."""
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:slow")
    grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    name = pod_name(grant["slot"], grant["lease_id"])
    await wait_until(lambda: kube.pod(name), 30, what="Pod create")
    hubctl.restart()
    row = await lease_row(grant["lease_id"])
    assert row["state"] == "ended" and row["end_reason"] in ("cancelled", "lost")
    await wait_until(lambda: kube.pod(name) is None, 30, what="Pod deletion")


async def test_booting_lease_without_a_pod_is_lost_on_restart(env, hubctl, profile, holder):
    """The window between insert_lease and Pod creation, injected via SQLite."""
    import uuid

    lease_id = uuid.uuid4().hex
    hubctl.scale(0)
    now = time.time()
    hubctl.sql(
        f"INSERT INTO leases VALUES ('{lease_id}', '{profile}', 0, '{holder}', 30, 'booting', {now}, NULL, NULL, NULL)"
    )
    hubctl.sql(f"UPDATE slots SET state='booting', lease_id='{lease_id}' WHERE slot=0")
    hubctl.scale(1)
    row = await lease_row(lease_id)
    assert row["state"] == "ended" and row["end_reason"] == "lost"


async def test_queued_callers_are_dropped_cleanly_by_a_kill(env, mcp, hubctl, profile, holder):
    for i in range(len(env.slot_ips)):
        await mcp.call("acquire", profile=profile, holder=f"{holder}-{i}", boot_wait_seconds=0)
    sessions = [McpHolder(env) for _ in range(2)]
    clients = [await s.__aenter__() for s in sessions]
    tasks = [
        asyncio.create_task(c.call("acquire", profile=profile, holder=f"{holder}-q{i}", wait_seconds=600))
        for i, c in enumerate(clients)
    ]
    await wait_until(lambda: _depth(mcp, 2), 30)
    hubctl.kill()
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 60)
    assert all(isinstance(r, BaseException) for r in results), results
    for s in sessions:
        await s.__aexit__()
    async with ui_client() as ui:
        st = (await ui.get("/api/status")).json()
    assert st["queue_depth"] == 0
    assert not any(s["state"] == "booting" and s["lease_id"] is None for s in st["slots"])


async def _depth(mcp, n):
    return (await mcp.call("status"))["queue_depth"] == n


async def test_pod_deleted_while_the_hub_is_down(env, mcp, kube, hubctl, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    hubctl.scale(0)
    kube.delete_pod(pod_name(grant["slot"], grant["lease_id"]), grace=0, wait=True)
    hubctl.scale(1)
    row = await lease_row(grant["lease_id"])
    assert row["state"] == "ended" and row["end_reason"] == "lost"


async def test_orphan_pods_are_deleted_and_strangers_are_not(env, kube, hubctl):
    hubctl.scale(0)

    def pod(name, labels):
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": NS, "labels": labels},
            "spec": {
                "terminationGracePeriodSeconds": 0,
                "containers": [{"name": "c", "image": kube.env.hub_image, "command": ["sleep", "3600"]}],
            },
        }

    kube.apply(pod("emu-slot-0-unknown0", {"app.kubernetes.io/name": "emulator", "emulator-hub/lease": "unknown"}))
    kube.apply(pod("emu-slot-1-nolabel0", {"app.kubernetes.io/name": "emulator"}))
    kube.apply(pod("stranger", {"app.kubernetes.io/name": "something-else"}))
    try:
        hubctl.scale(1)
        await wait_until(
            lambda: not ({"emu-slot-0-unknown0", "emu-slot-1-nolabel0"} & kube.pod_names()), 60, what="orphans"
        )
        assert kube.pod("stranger") is not None
    finally:
        kube.delete_pod("stranger", grace=0)


async def test_slot_pointing_at_an_ended_lease_is_freed(env, mcp, hubctl, profile, holder):
    grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    await mcp.call("release", lease_id=grant["lease_id"])
    hubctl.scale(0)
    hubctl.sql(f"UPDATE slots SET state='leased', lease_id='{grant['lease_id']}' WHERE slot=1")
    hubctl.sql("UPDATE slots SET state='booting', lease_id=NULL WHERE slot=0")
    hubctl.scale(1)
    async with ui_client() as ui:
        st = (await ui.get("/api/status")).json()
    assert [s["state"] for s in st["slots"]] == ["free"] * len(env.slot_ips)


async def test_lease_that_expired_while_down_is_reaped_promptly(env, mcp, hubctl, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder, ttl_minutes=1)
    hubctl.scale(0)
    await asyncio.sleep(max(0, grant["expires_at"] - time.time()) + 2)
    hubctl.scale(1)
    started = time.monotonic()

    async def ended():
        return (await lease_row(grant["lease_id"]))["state"] == "ended"

    await wait_until(ended, 15, what="reap after restart")
    assert time.monotonic() - started < 15
    assert (await lease_row(grant["lease_id"]))["end_reason"] == "expired"


async def test_kubernetes_api_outage_while_running(env, mcp, kube, hubctl, profile, holder):
    """The API server refuses the hub (its RoleBinding is gone): the reaper logs
    and keeps looping, reads still answer, acquire fails cleanly and frees the
    slot, and expiry resumes once the API answers again."""
    grant = await acquire_leased(mcp, env, profile, holder, ttl_minutes=1)
    rb = kube.json("-n", NS, "get", "rolebinding", "emulator-hub")
    for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
        rb["metadata"].pop(k, None)
    kube.run("-n", NS, "delete", "rolebinding", "emulator-hub")
    try:
        await wait_until(lambda: "reap failed" in hubctl.logs(), 60, interval=2, what="reap failure logged")
        async with ui_client() as ui:
            assert (await ui.get("/api/status")).status_code == 200
        assert (await mcp.call("status"))["slots"]
        with pytest.raises(McpError):
            await mcp.call("acquire", profile=profile, holder=f"{holder}-2", boot_wait_seconds=60)
        st = await mcp.call("status")
        assert sum(s["state"] != "free" for s in st["slots"]) <= 1  # only the original lease
        await asyncio.sleep(max(0, grant["expires_at"] - time.time()) + 5)
    finally:
        kube.apply(rb)
    # Expiry is decided from the database, so the lease still ends on time; its
    # Pod delete failed during the outage, and reconcile collects the orphan.
    row = await wait_until(lambda: _ended(grant["lease_id"]), 60, interval=2)
    assert row["end_reason"] == "expired"
    hubctl.restart()
    await wait_until(lambda: not kube.pod_names(), 60, what="reconcile to collect the orphan")


async def _ended(lease_id):
    row = await lease_row(lease_id)
    return row if row["state"] == "ended" else None


async def test_failed_pod_delete_still_frees_the_slot(env, mcp, kube, profile, holder):
    """Release while the hub may not delete Pods: the slot is handed off anyway
    (the finally in _end), and reconcile removes the orphan later."""
    name = await _release_without_delete_rights(env, mcp, kube, profile, holder)
    kube.delete_pod(name, grace=0)


async def _release_without_delete_rights(env, mcp, kube, profile, holder) -> str:
    grant = await acquire_leased(mcp, env, profile, holder)
    kube.run("-n", NS, "patch", "role", "emulator-hub", "--type=json",
             "-p", json.dumps([{"op": "replace", "path": "/rules/0/verbs", "value": ["create", "get", "list"]}]))  # fmt: skip
    try:
        with pytest.raises(McpError):
            await mcp.call("release", lease_id=grant["lease_id"])
        st = await mcp.call("status")
        assert st["slots"][grant["slot"]]["state"] == "free"
        assert kube.pod(pod_name(grant["slot"], grant["lease_id"])) is not None  # orphaned
        return pod_name(grant["slot"], grant["lease_id"])
    finally:
        kube.run("-n", NS, "patch", "role", "emulator-hub", "--type=json",
                 "-p", json.dumps([{"op": "replace", "path": "/rules/0/verbs",
                                    "value": ["create", "delete", "get", "list"]}]))  # fmt: skip


async def test_orphan_from_a_failed_delete_is_cleaned_by_reconcile(env, mcp, kube, hubctl, profile, holder):
    await _release_without_delete_rights(env, mcp, kube, profile, holder)
    hubctl.restart()
    await wait_until(lambda: not kube.pod_names(), 60, what="reconcile to delete the orphan")


async def test_database_survives_a_hard_kill(env, mcp, hubctl, profile, holder):
    grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    await mcp.call("release", lease_id=grant["lease_id"])
    body = {
        "form_factor": "tv",
        "system_image": "android-36-android-tv",
        "device": "tv_720p",
        "ram_mb": 1024,
        "cores": 1,
    }
    async with ui_client() as ui:
        assert (await ui.put("/api/profiles/durable", json=body)).status_code == 200
    hubctl.kill()
    try:
        assert (await lease_row(grant["lease_id"]))["end_reason"] == "released"
        async with ui_client() as ui:
            assert "durable" in {p["name"] for p in (await ui.get("/api/profiles")).json()}
        assert hubctl.sql("PRAGMA integrity_check") == [["ok"]]
    finally:
        async with ui_client() as ui:
            await ui.delete("/api/profiles/durable")


async def test_oom_killed_hub_recovers_consistently(env, mcp, kube, hubctl, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    hub = kube.hub_pod()["metadata"]["name"]
    # Exhaust memory inside the container to trip the cgroup OOM killer on PID 1's sibling;
    # simulate with SIGKILL from the node, which is exactly what the OOM killer delivers.
    kube.kill_container(hub)
    await wait_until(
        lambda: kube.hub_pod()["status"]["containerStatuses"][0]["restartCount"] >= 1, 120, what="container restart"
    )
    hubctl.wait_serving()
    row = await lease_row(grant["lease_id"])
    assert row["state"] == "leased"
    async with McpHolder(env) as m:
        assert (await m.call("heartbeat", lease_id=grant["lease_id"]))["state"] == "leased"


async def test_hub_moves_nodes_and_keeps_its_data(env, mcp, kube, hubctl, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    node = kube.hub_pod()["spec"]["nodeName"]
    kube.run("cordon", node)
    try:
        kube.run("drain", node, "--ignore-daemonsets", "--delete-emptydir-data", "--pod-selector",
                 "app.kubernetes.io/name=emulator-hub", "--timeout=120s", timeout=180)  # fmt: skip
        hubctl.wait_rollout()
        assert kube.hub_pod()["spec"]["nodeName"] != node
        row = await lease_row(grant["lease_id"])
        assert row["state"] == "leased"
        assert kube.pod(pod_name(grant["slot"], grant["lease_id"])) is not None
    finally:
        kube.run("uncordon", node)
    hubctl.restart()  # back onto the preferred hub node
