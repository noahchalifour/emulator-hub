"""ENG-338: the human-facing port, always through ingress-nginx and the (fake)
authentik forward-auth, never directly. REST with httpx, then the real page in
Chromium with Playwright."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from emulator_hub.catalog import DEVICES, SYSTEM_IMAGES
from tests.e2e.hub import UI_HOST, acquire_leased, pod_name, ui_client, wait_until, ws_url

AXE = Path("/opt/mcp-ts/node_modules/axe-core/axe.min.js")

# ------------------------------------------------------------------ auth


async def test_no_session_no_entry(env, mcp, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    async with ui_client(None) as anon:
        for path in ("/", "/index.html", "/app.js", "/api/me", "/api/status", "/api/profiles", "/api/catalog",
                     "/api/leases", f"/api/leases/{grant['lease_id']}/snapshot"):  # fmt: skip
            assert (await anon.get(path)).status_code == 401, path
        assert (await anon.post("/api/leases", json={"profile": profile})).status_code == 401
        assert (await anon.delete(f"/api/leases/{grant['lease_id']}")).status_code == 401
    import websockets
    import websockets.exceptions

    with pytest.raises(websockets.exceptions.InvalidStatus) as err:
        async with websockets.connect(ws_url(grant["lease_id"])):
            pass
    assert err.value.response.status_code == 401  # authentik refuses the upgrade at the ingress
    # The lease survived all of that.
    assert (await mcp.call("heartbeat", lease_id=grant["lease_id"]))["state"] == "leased"


async def test_healthz_is_open_through_the_ingress_only_by_auth(env):
    # /healthz is open on the hub itself, but the Ingress gates everything.
    async with ui_client(None) as anon:
        assert (await anon.get("/healthz")).status_code == 401
    async with ui_client() as ui:
        assert (await ui.get("/healthz")).json() == {"ok": True}


async def test_spoofed_authentik_header_cannot_impersonate(env):
    async with ui_client(None, headers={"X-authentik-username": "admin"}) as c:
        assert (await c.get("/api/me")).status_code == 401
    async with ui_client("mallory", headers={"X-authentik-username": "admin"}) as c:
        assert (await c.get("/api/me")).json() == {"user": "mallory"}  # nginx overwrote it


async def test_ui_port_is_unreachable_except_from_ingress_nginx(env, kube):
    for ns in ("e2e-client", "e2e-scratch"):
        code = kube.exec(
            "toolbox", "sh", "-c",
            "curl -s -o /dev/null -w '%{http_code}' -m 5 -H 'X-authentik-username: admin' "
            "http://emulator-hub.emulator-hub.svc.cluster.local:8080/api/me || true",
            ns=ns,
        )  # fmt: skip
        assert code.strip() in ("000", ""), f"{ns} reached the UI port directly: {code}"


async def test_me_and_catalog(env):
    async with ui_client("noah") as ui:
        assert (await ui.get("/api/me")).json() == {"user": "noah"}
        cat = (await ui.get("/api/catalog")).json()
    assert cat["devices"] == {k: list(v) for k, v in DEVICES.items()}
    assert cat["system_images"] == {
        k: {"api_level": v.api_level, "form_factors": list(v.form_factors)} for k, v in SYSTEM_IMAGES.items()
    }


# ------------------------------------------------------------------ profiles

GOOD = {"form_factor": "tablet", "system_image": "android-35-google-apis", "device": "pixel_tablet", "ram_mb": 2048,
        "cores": 2}  # fmt: skip


@pytest.mark.parametrize(
    "name,patch,message",
    [
        ("bad_name", {}, "name must be 1-40 characters"),
        ("x" * 41, {}, "name must be 1-40 characters"),
        ("ok-name", {"form_factor": "watch"}, "form_factor must be one of"),
        ("ok-name", {"system_image": "android-1"}, "system_image must be one of"),
        ("ok-name", {"form_factor": "tv", "device": "tv_1080p"}, "cannot boot a tv"),
        ("ok-name", {"device": "pixel_8"}, "device for a tablet must be one of"),
        ("ok-name", {"ram_mb": 1023}, "ram_mb must be between 1024 and 4096"),
        ("ok-name", {"ram_mb": 4097}, "ram_mb must be between 1024 and 4096"),
        ("ok-name", {"cores": 0}, "cores must be between 1 and 4"),
        ("ok-name", {"cores": 5}, "cores must be between 1 and 4"),
    ],
)
async def test_profile_validation(env, name, patch, message):
    async with ui_client() as ui:
        r = await ui.put(f"/api/profiles/{name}", json=GOOD | patch)
        assert r.status_code == 422 and message in r.json()["detail"], r.text
        assert name not in {p["name"] for p in (await ui.get("/api/profiles")).json()}


async def test_profile_crud(env):
    async with ui_client() as ui:
        assert (await ui.put("/api/profiles/zz-crud", json=GOOD)).json() == {"name": "zz-crud", **GOOD}
        upd = GOOD | {"device": "medium_tablet", "ram_mb": 3072}
        assert (await ui.put("/api/profiles/zz-crud", json=upd)).status_code == 200
        names = [p["name"] for p in (await ui.get("/api/profiles")).json()]
        assert names == sorted(names)
        row = next(p for p in (await ui.get("/api/profiles")).json() if p["name"] == "zz-crud")
        assert row["device"] == "medium_tablet" and row["ram_mb"] == 3072
        assert (await ui.delete("/api/profiles/zz-crud")).status_code == 204
        assert (await ui.delete("/api/profiles/zz-crud")).status_code == 404
        assert "zz-crud" not in {p["name"] for p in (await ui.get("/api/profiles")).json()}


async def test_profile_in_use_cannot_be_deleted(env, mcp, holder):
    async with ui_client() as ui:
        body = {"form_factor": "phone", "system_image": "android-35-google-apis", "device": "pixel_8",
                "ram_mb": 2048 if env.real else 1024, "cores": 2 if env.real else 1}  # fmt: skip
        assert (await ui.put("/api/profiles/in-use", json=body)).status_code == 200
        grant = await mcp.call("acquire", profile="in-use", holder=holder, boot_wait_seconds=0)
        r = await ui.delete("/api/profiles/in-use")
        assert r.status_code == 422 and "release it first" in r.json()["detail"]
        # A UI-created profile is really bootable.
        from tests.e2e.hub import wait_leased

        grant = await wait_leased(mcp, env, grant)
        await mcp.call("release", lease_id=grant["lease_id"])
        assert (await ui.delete("/api/profiles/in-use")).status_code == 204


# ------------------------------------------------------------------ leases over REST


async def test_rest_lease_attribution_released_and_forced(env, mcp, profile, holder):
    async with ui_client("alice", timeout=env.boot_budget_s) as alice, ui_client("bob") as bob:
        mine = (await alice.post("/api/leases", json={"profile": profile})).json()
        st = (await alice.get("/api/status")).json()
        assert st["slots"][mine["slot"]]["holder"] == "ui:alice"
        assert (await alice.delete(f"/api/leases/{mine['lease_id']}")).json()["end_reason"] == "released"
        theirs = (await alice.post("/api/leases", json={"profile": profile})).json()
        assert (await bob.delete(f"/api/leases/{theirs['lease_id']}")).json()["end_reason"] == "forced"
        agent = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
        assert (await bob.delete(f"/api/leases/{agent['lease_id']}")).json()["end_reason"] == "forced"
        hist = {r["id"]: r for r in (await bob.get("/api/leases")).json()}
    assert hist[mine["lease_id"]]["end_reason"] == "released" and hist[mine["lease_id"]]["holder"] == "ui:alice"
    assert hist[theirs["lease_id"]]["end_reason"] == "forced"
    assert hist[agent["lease_id"]]["end_reason"] == "forced"


async def test_history_order_and_limits(env, mcp, profile, holder):
    for i in range(3):
        g = await mcp.call("acquire", profile=profile, holder=f"{holder}-{i}", boot_wait_seconds=0)
        await mcp.call("release", lease_id=g["lease_id"])
    async with ui_client() as ui:
        rows = (await ui.get("/api/leases")).json()
        assert [r["created_at"] for r in rows] == sorted((r["created_at"] for r in rows), reverse=True)
        assert [r["holder"] for r in rows[:3]] == [f"{holder}-2", f"{holder}-1", f"{holder}-0"]
        assert len(rows) <= 100
        assert len((await ui.get("/api/leases?limit=2")).json()) == 2
        assert len((await ui.get("/api/leases?limit=100000")).json()) <= 500


async def test_rest_error_mapping(env, mcp, kube, hub_env, profile, holder):
    async with ui_client(timeout=120) as ui:
        assert (await ui.post("/api/leases/nope/heartbeat")).status_code == 404
        g = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
        await mcp.call("release", lease_id=g["lease_id"])
        r = await ui.post(f"/api/leases/{g['lease_id']}/heartbeat")
        assert r.status_code == 409 and "acquire a new one" in r.json()["detail"]
        assert (await ui.delete(f"/api/leases/{g['lease_id']}")).status_code == 409
        r = await ui.post("/api/leases", json={"profile": profile, "wait_seconds": 601})
        assert r.status_code == 422  # pydantic bound
        hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:exit-during-boot")
    async with ui_client(timeout=120) as ui:
        r = await ui.post("/api/leases", json={"profile": profile})
        assert r.status_code == 502 and "exited during boot" in r.json()["detail"]


@pytest.mark.xfail(
    strict=True,
    reason="POST /api/leases blocks for the whole cold boot (no boot_wait_seconds); ingress-nginx's 60s "
    "proxy-read-timeout cuts it off with a 504 (ENG-338)",
)
async def test_ui_boot_of_a_slow_cold_emulator_survives_the_ingress(env, hub_env, profile):
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:slow")  # 75s boot, like a cold emulator
    async with ui_client(timeout=300) as ui:
        r = await ui.post("/api/leases", json={"profile": profile})
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
    assert r.json()["state"] in ("booting", "leased")


# ------------------------------------------------------------------ browser


@pytest.fixture
async def browser():
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        try:
            yield b
        finally:
            await b.close()


async def page_for(browser, user: str):
    ctx = await browser.new_context(base_url=f"http://{UI_HOST}")
    await ctx.add_cookies([{"name": "e2e_user", "value": user, "domain": UI_HOST, "path": "/"}])
    page = await ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.e2e_errors = errors  # type: ignore[attr-defined]
    await page.goto("/")
    await expect(page.locator("#whoami")).to_have_text(user)
    return page


async def test_page_loads_cleanly(env, browser):
    page = await page_for(browser, "noah")
    for asset in ("/tokens.css", "/app.css", "/app.js", "/fonts/InterVariable.woff2"):
        r = await page.request.get(asset)
        assert r.ok, asset
    await expect(page.locator("#slots .card")).to_have_count(len(env.slot_ips))
    await expect(page.locator("#summary")).to_have_text(f"0 of {len(env.slot_ips)} in use")
    assert page.e2e_errors == []


async def test_devices_tab_follows_a_lease_without_reloading(env, mcp, browser, profile, holder):
    page = await page_for(browser, "noah")
    grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    card = page.locator("#slots .card").nth(grant["slot"])
    await expect(card.locator(".badge")).to_have_text("booting", timeout=10_000)
    await expect(card.locator(".badge")).to_have_text("leased", timeout=env.boot_budget_s * 1000)
    await expect(card).to_contain_text(holder)
    await expect(card.locator("img")).to_be_visible()
    await mcp.call("release", lease_id=grant["lease_id"])
    await expect(card.locator(".badge")).to_have_text("free", timeout=10_000)
    assert page.e2e_errors == []


async def test_boot_form_and_release_own_lease(env, browser, profile):
    page = await page_for(browser, "noah")
    await page.select_option("#boot-profile", profile)
    await page.click("#boot-form button[type=submit]")
    card = page.locator("#slots .card", has_text="ui:noah")
    await expect(card.locator(".badge")).to_have_text("leased", timeout=env.boot_budget_s * 1000)
    await card.get_by_role("button", name="Release").click()
    await expect(page.locator("#slots .card", has_text="ui:noah")).to_have_count(0, timeout=10_000)
    async with ui_client() as ui:
        assert (await ui.get("/api/leases?limit=1")).json()[0]["end_reason"] == "released"


async def test_force_release_from_another_user_confirms(env, browser, profile):
    alice = await page_for(browser, "alice")
    await alice.select_option("#boot-profile", profile)
    await alice.click("#boot-form button[type=submit]")
    await expect(alice.locator("#slots .card", has_text="ui:alice").locator(".badge")).to_have_text(
        "leased", timeout=env.boot_budget_s * 1000
    )
    bob = await page_for(browser, "bob")
    card = bob.locator("#slots .card", has_text="ui:alice")
    await expect(card).to_have_count(1)
    # Another UI user's lease is released without a confirm() (only agents' leases ask).
    await card.get_by_role("button", name="Release").click()
    await expect(bob.locator("#slots .card", has_text="ui:alice")).to_have_count(0, timeout=10_000)
    async with ui_client() as ui:
        assert (await ui.get("/api/leases?limit=1")).json()[0]["end_reason"] == "forced"


async def test_force_release_of_an_agent_lease_needs_confirmation(env, mcp, browser, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    page = await page_for(browser, "noah")
    card = page.locator("#slots .card", has_text=holder)
    dialogs: list[str] = []

    async def dismiss(d):
        dialogs.append(d.message)
        await d.dismiss()

    page.on("dialog", dismiss)
    await card.get_by_role("button", name="Release").click()
    await asyncio.sleep(1)
    assert dialogs and holder in dialogs[0]
    assert (await mcp.call("heartbeat", lease_id=grant["lease_id"]))["state"] == "leased"
    page.remove_listener("dialog", dismiss)
    page.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
    await card.get_by_role("button", name="Release").click()
    await expect(page.locator("#slots .card", has_text=holder)).to_have_count(0, timeout=10_000)


async def test_busy_shows_the_queue_position(env, mcp, browser, profile, holder):
    for i in range(len(env.slot_ips)):
        await mcp.call("acquire", profile=profile, holder=f"{holder}-{i}", boot_wait_seconds=0)
    page = await page_for(browser, "noah")
    await page.select_option("#boot-profile", profile)
    await page.click("#boot-form button[type=submit]")
    await expect(page.locator("#error")).to_have_text("no emulator slot free; you were number 1 in the queue")


async def test_profiles_tab_filters_and_round_trips(env, browser):
    page = await page_for(browser, "noah")
    await page.get_by_role("tab", name="Profiles").click()
    form = page.locator("#profile-form")
    for ff, devices in DEVICES.items():
        await form.locator("select[name=form_factor]").select_option(ff)
        images = [k for k, v in SYSTEM_IMAGES.items() if ff in v.form_factors]
        await expect(form.locator("select[name=system_image] option")).to_have_text(images)
        await expect(form.locator("select[name=device] option")).to_have_text(list(devices))
    await form.locator("input[name=name]").fill("ui-made")
    await form.locator("select[name=form_factor]").select_option("tv")
    await form.locator("select[name=device]").select_option("tv_720p")
    await form.locator("input[name=ram_mb]").fill("1024")
    await form.locator("input[name=cores]").fill("1")
    await form.get_by_role("button", name="Save profile").click()
    row = page.locator("#profile-rows tr", has_text="ui-made")
    await expect(row).to_contain_text("tv_720p")
    # Edit fills the form; change cores and save.
    await row.get_by_role("button", name="Edit").click()
    await expect(form.locator("input[name=name]")).to_have_value("ui-made")
    await form.locator("input[name=cores]").fill("2")
    await form.get_by_role("button", name="Save profile").click()
    await expect(row.locator("td").nth(5)).to_have_text("2")
    await row.get_by_role("button", name="Delete").click()
    await expect(page.locator("#profile-rows tr", has_text="ui-made")).to_have_count(0)


async def test_profile_errors_show_in_the_banner(env, mcp, browser, holder):
    page = await page_for(browser, "noah")
    await page.get_by_role("tab", name="Profiles").click()
    form = page.locator("#profile-form")
    # Bypass the browser's own min/max to reach the server's validation.
    await form.locator("input[name=ram_mb]").evaluate("e => { e.removeAttribute('min'); e.value = '512'; }")
    await form.locator("input[name=name]").fill("too-small")
    await form.get_by_role("button", name="Save profile").click()
    await expect(page.locator("#error")).to_have_text("ram_mb must be between 1024 and 4096")
    # Deleting an in-use profile says why.
    grant = await mcp.call("acquire", profile="e2e", holder=holder, boot_wait_seconds=0)
    assert grant
    await page.locator("#profile-rows tr", has_text="e2e").first.get_by_role("button", name="Delete").click()
    await expect(page.locator("#error")).to_contain_text("release it first")


async def test_history_tab(env, mcp, browser, profile, holder):
    g = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    await mcp.call("release", lease_id=g["lease_id"])
    page = await page_for(browser, "noah")
    await page.get_by_role("tab", name="History").click()
    first = page.locator("#history-rows tr").first
    await expect(first).to_contain_text(holder)
    await expect(first).to_contain_text("released")
    await expect(first).to_contain_text(profile)


async def test_network_failure_shows_a_readable_error(env, browser):
    page = await page_for(browser, "noah")
    await page.route("**/api/status", lambda route: route.fulfill(status=502, body='{"detail": "upstream down"}'))
    await expect(page.locator("#error")).to_have_text("upstream down", timeout=10_000)
    await page.unroute("**/api/status")
    await page.route("**/api/status", lambda route: route.abort())
    await expect(page.locator("#error")).not_to_be_empty(timeout=10_000)
    await expect(page.locator("header .brand")).to_be_visible()  # the page is intact


async def test_accessibility_smoke(env, browser):
    if not AXE.exists():
        pytest.skip("axe-core is installed in the runner image only")
    page = await page_for(browser, "noah")
    for tab in ("Devices", "Profiles", "History"):
        await page.get_by_role("tab", name=tab).click()
        await page.add_script_tag(path=str(AXE))
        result = await page.evaluate("async () => await axe.run(document, {resultTypes: ['violations']})")
        critical = [v["id"] for v in result["violations"] if v["impact"] == "critical"]
        assert critical == [], f"{tab}: {json.dumps(critical)}"
    # Keyboard reaches the tabs and the boot form.
    await page.get_by_role("tab", name="Devices").click()
    seen = set()
    for _ in range(15):
        await page.keyboard.press("Tab")
        seen.add(await page.evaluate("document.activeElement.id || document.activeElement.textContent.trim()"))
    assert {"Devices", "Profiles", "History"} <= seen
    assert "boot-profile" in seen


async def test_live_view_url_follows_https(env, mcp, profile, holder):
    """Behind TLS (X-Forwarded-Proto https) the page opens wss://."""
    grant = await acquire_leased(mcp, env, profile, holder)
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        ctx = await b.new_context(base_url=f"https://{UI_HOST}", ignore_https_errors=True)
        await ctx.add_cookies([{"name": "e2e_user", "value": "noah", "domain": UI_HOST, "path": "/"}])
        page = await ctx.new_page()
        sockets: list[str] = []
        page.on("websocket", lambda ws: sockets.append(ws.url))
        await page.goto("/")
        await page.locator("#slots .card", has_text=holder).get_by_role("button", name="Open live view").click()
        await wait_until(lambda: sockets, 15, what="websocket")
        await b.close()
    assert sockets[0] == f"wss://{UI_HOST}/api/leases/{grant['lease_id']}/live"
    assert pod_name(grant["slot"], grant["lease_id"])  # lease still referenced


async def test_https_ingress_serves_the_api(env):
    async with httpx.AsyncClient(base_url=f"https://{UI_HOST}", verify=False, cookies={"e2e_user": "noah"}) as c:
        assert (await c.get("/api/me")).json() == {"user": "noah"}
