"""ENG-337: the agent-facing contract on the machine port, over real
streamable HTTP through the MCP Ingress, with the official SDK clients."""

import asyncio
import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from tests.e2e.hub import (
    MCP_HOST,
    McpError,
    acquire_leased,
    jsonrpc,
    mcp_raw_client,
    mcp_session,
    wait_until,
)

SNAPSHOT = Path(__file__).with_name("mcp_tools.snapshot.json")
INIT = jsonrpc(
    "initialize",
    {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e-raw", "version": "1"}},
)

# ------------------------------------------------------------------ transport and auth


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Bearer wrong",
        "Bearer",
        "Basic ZTJlOmUyZQ==",
        "bearer {token}",
        "Bearer {token}x",
        "Bearer {token_prefix}",
        "Bearer  {token}",
        "Bearer Bearer {token}",
    ],
)
async def test_mcp_rejects_anything_but_the_exact_bearer(env, authorization):
    token = authorization.format(token=env.api_token, token_prefix=env.api_token[:-1]) if authorization else None
    async with mcp_raw_client(env, token) as c:
        r = await c.post("/mcp", json=INIT)
    assert r.status_code == 401
    assert r.json() == {"detail": "missing or invalid bearer token"}


async def test_authentik_header_means_nothing_on_the_machine_port(env):
    async with mcp_raw_client(env, None) as c:
        c.headers["X-authentik-username"] = "noah"
        assert (await c.post("/mcp", json=INIT)).status_code == 401
        assert (await c.get("/api/status")).status_code == 401


async def test_only_healthz_and_metrics_are_open(env):
    async with mcp_raw_client(env, None) as c:
        assert (await c.get("/healthz")).json() == {"ok": True}
        assert (await c.get("/metrics")).status_code == 200
        for path in ("/", "/mcp", "/api/status", "/docs", "/openapi.json", "/metrics/../mcp"):
            assert (await c.get(path)).status_code == 401, path


async def test_stateless_json_mode_needs_no_session(env):
    async with mcp_raw_client(env, f"Bearer {env.api_token}") as c:
        init = await c.post("/mcp", json=INIT)
        assert init.status_code == 200
        assert init.headers["content-type"].startswith("application/json")
        body = init.json()
        assert "workflow" in body["result"]["instructions"].lower()
        assert "heartbeat" in body["result"]["instructions"]
        # No Mcp-Session-Id, no initialized notification: a bare call works.
        r = await c.post("/mcp", json=jsonrpc("tools/call", {"name": "status", "arguments": {}}, id=7))
        assert r.status_code == 200 and r.json()["id"] == 7
        assert len(r.json()["result"]["structuredContent"]["slots"]) == len(env.slot_ips)


async def test_foreign_host_header_is_accepted(env):
    """DNS-rebinding protection is off on purpose: host checks are the Ingress's job."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"http://{MCP_HOST}/mcp",
            json=INIT,
            headers={
                "Authorization": f"Bearer {env.api_token}",
                "Accept": "application/json, text/event-stream",
                "Origin": "http://evil.example",
            },
        )
    assert r.status_code == 200


async def test_initialize_carries_the_instructions(env):
    async with mcp_session(env) as m:
        text = m.client.instructions or ""
    for needle in ("list_profiles", "acquire(profile, holder)", "heartbeat(lease_id)", "release(lease_id)", "adb"):
        assert needle in text


# ------------------------------------------------------------------ tool surface


async def test_tool_surface_matches_the_snapshot(env, mcp):
    tools = (await mcp.client.list_tools()).tools
    surface = {
        t.name: {"description": t.description, "input_schema": t.input_schema, "output_schema": t.output_schema}
        for t in sorted(tools, key=lambda t: t.name)
    }
    assert sorted(surface) == ["acquire", "heartbeat", "list_profiles", "release", "status"]
    if not SNAPSHOT.exists():  # pragma: no cover - regenerate with E2E_UPDATE_SNAPSHOT=1
        SNAPSHOT.write_text(json.dumps(surface, indent=2, sort_keys=True) + "\n")
    expected = json.loads(SNAPSHOT.read_text())
    assert surface == expected, "MCP tool surface changed; update tests/e2e/mcp_tools.snapshot.json deliberately"


async def test_list_profiles_sees_ui_edits_immediately(env, mcp):
    from tests.e2e.hub import ui_client

    body = {
        "form_factor": "tv",
        "system_image": "android-36-android-tv",
        "device": "tv_720p",
        "ram_mb": 1024,
        "cores": 1,
    }
    async with ui_client() as ui:
        assert (await ui.put("/api/profiles/mcp-sees-me", json=body)).status_code == 200
        try:
            listed = await mcp.call("list_profiles")
            assert {"name": "mcp-sees-me", **body} in listed["profiles"]
            assert listed["free_slots"] == len(env.slot_ips)
            assert listed["queue_depth"] == 0
        finally:
            await ui.delete("/api/profiles/mcp-sees-me")
    assert "mcp-sees-me" not in {p["name"] for p in (await mcp.call("list_profiles"))["profiles"]}


async def test_status_shows_active_and_free_slots(env, mcp, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    st = await mcp.call("status")
    active = st["slots"][grant["slot"]]
    assert active == {
        "slot": grant["slot"],
        "state": "leased",
        "lease_id": grant["lease_id"],
        "profile": profile,
        "holder": holder,
        "expires_at": grant["expires_at"],
    }
    for s in st["slots"]:
        if s["slot"] != grant["slot"]:
            assert s == {
                "slot": s["slot"],
                "state": "free",
                "lease_id": None,
                "profile": None,
                "holder": None,
                "expires_at": None,
            }


# ------------------------------------------------------------------ behaviour


@pytest.mark.android
async def test_full_agent_flow(env, mcp, adb, profile, holder):
    profiles = await mcp.call("list_profiles")
    assert profile in {p["name"] for p in profiles["profiles"]}
    grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    assert set(grant) == {"lease_id", "state", "profile", "slot", "adb", "in_cluster", "expires_at"}
    while grant["state"] == "booting":
        await asyncio.sleep(2)
        grant = await mcp.call("heartbeat", lease_id=grant["lease_id"])
    if env.real:
        adb.connect(grant["adb"])
        assert adb.shell(grant["adb"], "getprop sys.boot_completed").strip() == "1"
    hb = await mcp.call("heartbeat", lease_id=grant["lease_id"])
    assert hb["expires_at"] >= grant["expires_at"]
    rel = await mcp.call("release", lease_id=grant["lease_id"])
    assert rel == {"lease_id": grant["lease_id"], "state": "ended", "end_reason": "released"}


@pytest.mark.android
async def test_default_acquire_answers_well_inside_client_timeouts(env, mcp, hub_env, profile, holder):
    """Regression for #1: a cold boot longer than the default 30s boot wait
    still answers at ~30s with a booting grant."""
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:slow")
    started = time.monotonic()
    grant = await mcp.call("acquire", profile=profile, holder=holder)
    took = time.monotonic() - started
    assert grant["state"] == "booting"
    assert 28 <= took < 45, took


async def test_boot_wait_seconds_is_clamped(env, mcp, hub_env, profile, holder):
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:slow")
    started = time.monotonic()
    grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=-5)
    assert grant["state"] == "booting" and time.monotonic() - started < 5
    await mcp.call("release", lease_id=grant["lease_id"])


async def test_every_hub_error_is_a_readable_tool_error(env, mcp, hub_env, profile, holder):
    cases = [
        ("acquire", dict(profile="nope", holder="x"), "no profile named 'nope'"),
        ("acquire", dict(profile=profile, holder="x", ttl_minutes=500), "between 1 and 120"),
        ("acquire", dict(profile=profile, holder=""), "holder must say who you are"),
        ("heartbeat", dict(lease_id="nope"), "no lease 'nope'"),
        ("release", dict(lease_id="nope"), "no lease 'nope'"),
    ]
    for tool, args, text in cases:
        result = await mcp.call_raw(tool, **args)
        assert result.is_error, (tool, args)
        assert text in result.content[0].text
    grant = await acquire_leased(mcp, env, profile, holder, ttl_minutes=1)
    await mcp.call("release", lease_id=grant["lease_id"])
    again = await mcp.call_raw("release", lease_id=grant["lease_id"])
    assert again.is_error and "already ended (released)" in again.content[0].text
    hb = await mcp.call_raw("heartbeat", lease_id=grant["lease_id"])
    assert hb.is_error and "is ended (released); acquire a new one" in hb.content[0].text
    # Busy with a position.
    fills = [
        await mcp.call("acquire", profile=profile, holder=f"{holder}-{i}", boot_wait_seconds=0)
        for i in range(len(env.slot_ips))
    ]
    busy = await mcp.call_raw("acquire", profile=profile, holder=holder, wait_seconds=0)
    assert busy.is_error and "you were number 1 in the queue" in busy.content[0].text
    for f in fills:
        await mcp.call("release", lease_id=f["lease_id"])
    # boot_failed carries the reason.
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:exit-during-boot")
    failed = await mcp.call_raw("acquire", profile=profile, holder=holder, boot_wait_seconds=120)
    assert failed.is_error and "exited during boot" in failed.content[0].text


async def test_expired_lease_heartbeat_says_so(env, mcp, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder, ttl_minutes=1)

    # Watch status (a heartbeat would extend it), then heartbeat once.
    async def gone():
        return (await mcp.call("status"))["slots"][grant["slot"]]["lease_id"] != grant["lease_id"]

    await wait_until(gone, 120, interval=2, what="expiry")
    r = await mcp.call_raw("heartbeat", lease_id=grant["lease_id"])
    assert r.is_error and "is ended (expired); acquire a new one" in r.content[0].text


async def test_kubernetes_outage_during_acquire_is_survivable(env, mcp, kube, profile, holder):
    """Revoke the hub's Pod permissions: acquire fails cleanly, the hub keeps serving."""
    rb = kube.json("-n", "emulator-hub", "get", "rolebinding", "emulator-hub")
    kube.run("-n", "emulator-hub", "delete", "rolebinding", "emulator-hub")
    try:
        # The hub's ApiError is not a HubError: the agent gets a tool error
        # (not a dropped connection), and the slot is freed.
        with pytest.raises(McpError, match="Error executing tool acquire"):
            await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=30)
        st = await mcp.call("status")
        assert all(s["state"] == "free" for s in st["slots"])
    finally:
        for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
            rb["metadata"].pop(k, None)
        kube.apply(rb)
    grant = await acquire_leased(mcp, env, profile, holder)
    assert grant["state"] == "leased"


async def test_bad_argument_types_are_validation_errors(env):
    async with mcp_raw_client(env, f"Bearer {env.api_token}") as c:
        for args in ({"profile": "e2e", "holder": "x", "ttl_minutes": "abc"}, {"profile": "e2e"}):
            r = await c.post("/mcp", json=jsonrpc("tools/call", {"name": "acquire", "arguments": args}))
            assert r.status_code == 200, r.text
            body = r.json()
            err_text = json.dumps(body)
            assert ("error" in body) or body["result"]["isError"], body
            assert "validation" in err_text.lower() or "required" in err_text.lower() or "int" in err_text.lower()


@pytest.mark.android
def test_typescript_sdk_runs_the_same_flow(env, e2e_profiles, profile):
    ts = Path("/opt/mcp-ts")
    if not ts.exists():
        pytest.skip("TypeScript SDK is installed in the runner image only")
    # ESM resolves packages next to the script, so run a copy inside /opt/mcp-ts.
    script = ts / "flow.mjs"
    script.write_text((Path(__file__).resolve().parents[2] / "e2e" / "mcp-ts" / "flow.mjs").read_text())
    res = subprocess.run(
        ["node", str(script), f"http://{MCP_HOST}/mcp", env.api_token, profile],
        capture_output=True,
        text=True,
        timeout=env.boot_budget_s + 120,
        cwd=ts,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    assert res.returncode == 0, res.stderr
    steps = {s["step"]: s["value"] for s in map(json.loads, res.stdout.splitlines())}
    assert steps["tools"] == ["acquire", "heartbeat", "list_profiles", "release", "status"]
    assert "heartbeat" in steps["instructions"]
    assert steps["leased"]["state"] == "leased"
    assert steps["error"]["isError"] and "no lease 'nope'" in steps["error"]["text"]
    assert steps["release"]["end_reason"] == "released"


def test_raw_curl_json_rpc(env):
    out = subprocess.run(
        [
            "curl", "-sS", "-X", "POST", f"http://{MCP_HOST}/mcp",
            "-H", f"Authorization: Bearer {env.api_token}",
            "-H", "Content-Type: application/json",
            "-H", "Accept: application/json, text/event-stream",
            "-d", json.dumps(jsonrpc("tools/call", {"name": "list_profiles", "arguments": {}})),
        ],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout  # fmt: skip
    assert "e2e" in {p["name"] for p in json.loads(out)["result"]["structuredContent"]["profiles"]}


async def test_status_stays_responsive_during_a_boot(env, mcp, hub_env, profile, holder):
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:slow")
    booting = asyncio.create_task(mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=600))
    await asyncio.sleep(2)

    async def one():
        async with mcp_session(env) as m:
            started = time.monotonic()
            await m.call("status")
            return time.monotonic() - started

    latencies = await asyncio.gather(*(one() for _ in range(20)))
    assert max(latencies) < 10, latencies
    assert not booting.done()
    booting.cancel()
    await asyncio.gather(booting, return_exceptions=True)
