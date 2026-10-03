"""ENG-341 and ENG-334: properties of the live Pods, Services, NetworkPolicies,
RBAC and metrics, plus deploy/startup/upgrade behaviour of the hub Deployment."""

import asyncio
import json
import subprocess
import time

import pytest

from tests.e2e.hub import (
    NS,
    McpError,
    McpHolder,
    acquire_leased,
    metric,
    metrics_text,
    pod_name,
    tcp_banner,
    ui_client,
    wait_until,
)

# ================================================================== emulator Pods (ENG-341)


@pytest.mark.android
async def test_running_pod_is_hardened(env, mcp, kube, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    name = pod_name(grant["slot"], grant["lease_id"])
    pod = kube.pod(name)
    spec = pod["spec"]
    c = spec["containers"][0]
    assert c["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10010,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert "privileged" not in c["securityContext"]
    assert spec["automountServiceAccountToken"] is False and spec["enableServiceLinks"] is False
    assert not any(v.get("projected") for v in spec["volumes"])
    status = kube.exec(name, "cat", "/proc/1/status")
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    assert fields["Uid"].split()[0] == "10010"
    assert int(fields["CapEff"].strip(), 16) == 0
    assert fields["NoNewPrivs"].strip() == "1"
    assert fields["Seccomp"].strip() == "2"
    assert "993" in fields["Groups"].split()
    assert not kube.exec(name, "sh", "-c", "ls /var/run/secrets/kubernetes.io 2>/dev/null || true").strip()


@pytest.mark.android
async def test_kvm_comes_from_the_device_plugin(env, mcp, kube, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    name = pod_name(grant["slot"], grant["lease_id"])
    pod = kube.pod(name)
    c = pod["spec"]["containers"][0]
    assert c["resources"]["requests"]["squat.ai/kvm"] == "1" and c["resources"]["limits"]["squat.ai/kvm"] == "1"
    assert pod["spec"]["securityContext"]["supplementalGroups"] == [993]
    ls = kube.exec(name, "ls", "-ln", "/dev/kvm")
    assert ls.startswith("crw-rw")
    if not env.real:  # the real job keeps the host device's own mode
        assert ls.split()[3] == "993" and ls.startswith("crw-rw----")
    kube.exec(name, "sh", "-c", "test -w /dev/kvm")
    if env.real:
        out = kube.exec(name, "emulator", "-accel-check", check=False)
        assert "KVM" in out and "is installed and usable" in out, out


async def test_scheduling_is_kvm_nodes_only_and_capacity_bound(env, mcp, kube, hub_env, profile, holder):
    if env.kvm_capacity >= len(env.slot_ips):
        hub_env(HUB_BOOT_TIMEOUT_S="20")
        # Shrink the node's advertised kvm capacity below the slot count.
        kube.run("-n", "kube-system", "patch", "ds", "generic-device-plugin", "--type=json", "-p", json.dumps([
            {"op": "replace", "path": "/spec/template/spec/containers/0/args/3",
             "value": f"name: kvm\ngroups:\n  - count: {len(env.slot_ips) - 1}\n    paths:\n      - path: /dev/kvm\n"}
        ]))  # fmt: skip
        kube.run("-n", "kube-system", "rollout", "status", "ds/generic-device-plugin", "--timeout=120s")
        await wait_until(
            lambda: (
                kube.json("get", "node", env.kvm_node)["status"]["allocatable"].get("squat.ai/kvm")
                == str(len(env.slot_ips) - 1)
            ),
            60,
        )
    try:
        grants = [
            await mcp.call("acquire", profile=profile, holder=f"{holder}-{i}", boot_wait_seconds=0)
            for i in range(len(env.slot_ips))
        ]
        for g in grants:
            await wait_until(lambda g=g: kube.pod(pod_name(g["slot"], g["lease_id"])), 30)
        pods = [kube.pod(pod_name(g["slot"], g["lease_id"])) for g in grants]
        await asyncio.sleep(5)
        pods = [kube.pod(pod_name(g["slot"], g["lease_id"])) for g in grants]
        for p in pods:
            if p and p["spec"].get("nodeName"):
                assert p["spec"]["nodeName"] == env.kvm_node
        pending = [p for p in pods if p and p["status"]["phase"] == "Pending"]
        assert len(pending) == 1, [p["status"]["phase"] for p in pods if p]
        reason = pending[0]["status"]["conditions"][0].get("message", "")
        assert "squat.ai/kvm" in reason
        stuck = next(g for g in grants if pod_name(g["slot"], g["lease_id"]) == pending[0]["metadata"]["name"])
        hb_err = await wait_until(lambda: _hb_error(mcp, stuck["lease_id"]), 90, interval=2)
        assert "(boot_failed)" in hb_err
    finally:
        kube.run("-n", "kube-system", "patch", "ds", "generic-device-plugin", "--type=json", "-p", json.dumps([
            {"op": "replace", "path": "/spec/template/spec/containers/0/args/3",
             "value": f"name: kvm\ngroups:\n  - count: {env.kvm_capacity}\n    paths:\n      - path: /dev/kvm\n"}
        ]))  # fmt: skip
        kube.run("-n", "kube-system", "rollout", "status", "ds/generic-device-plugin", "--timeout=120s")
        await wait_until(
            lambda: (
                kube.json("get", "node", env.kvm_node)["status"]["allocatable"].get("squat.ai/kvm")
                == str(env.kvm_capacity)
            ),
            60,
        )


async def _hb_error(mcp, lease_id):
    try:
        await mcp.call("heartbeat", lease_id=lease_id)
    except McpError as exc:
        return str(exc)


@pytest.mark.android
async def test_resources_requests_and_limits(env, mcp, kube, profile, holder, e2e_profiles):
    grant = await acquire_leased(mcp, env, profile, holder)
    pod = kube.pod(pod_name(grant["slot"], grant["lease_id"]))
    from lightkube.utils.quantity import parse_quantity

    from emulator_hub.models import Profile
    from emulator_hub.pods import memory_mb

    cores = e2e_profiles[profile]["cores"]
    request_mb, limit_mb = memory_mb(Profile(profile, **e2e_profiles[profile]))
    res = pod["spec"]["containers"][0]["resources"]
    assert res["requests"]["cpu"] == str(cores)
    # The API server normalises quantities (2048Mi reads back as 2Gi).
    assert parse_quantity(res["requests"]["memory"]) == request_mb * 2**20
    assert parse_quantity(res["limits"]["memory"]) == limit_mb * 2**20
    assert "cpu" not in res["limits"]


@pytest.mark.real_emulator
@pytest.mark.parametrize("ram_mb,cores", [(4096, 4), (1024, 2)], ids=["largest", "smallest-ram"])
async def test_extreme_profiles_boot_without_oom(env, mcp, kube, adb, holder, ram_mb, cores):
    """The largest profile and the smallest RAM boot without an OOMKill. (A
    1-core emulator is too slow to boot under nested virtualisation on CI.)"""
    from lightkube.utils.quantity import parse_quantity

    from tests.e2e.hub import APK

    allocatable = float(parse_quantity(kube.json("get", "node", env.kvm_node)["status"]["allocatable"]["cpu"]))
    # Leave room for the node's own DaemonSets (device plugin, kube-proxy, speaker).
    if cores > allocatable - 0.5:
        pytest.skip(f"the KVM node has {allocatable} allocatable CPUs, the profile requests {cores}")

    body = {"form_factor": "phone", "system_image": "android-35-google-apis", "device": "medium_phone",
            "ram_mb": ram_mb, "cores": cores}  # fmt: skip
    async with ui_client() as ui:
        assert (await ui.put(f"/api/profiles/x-{ram_mb}", json=body)).status_code == 200
    try:
        grant = await acquire_leased(mcp, env, f"x-{ram_mb}", holder)
        adb.connect(grant["adb"])
        adb.wait_boot_completed(grant["adb"])
        adb.install(grant["adb"], APK)
        pod = kube.pod(pod_name(grant["slot"], grant["lease_id"]))
        status = pod["status"]["containerStatuses"][0]
        assert status["restartCount"] == 0 and "terminated" not in status.get("lastState", {})
        await mcp.call("release", lease_id=grant["lease_id"])
    finally:
        async with ui_client() as ui:
            await ui.delete(f"/api/profiles/x-{ram_mb}")


async def test_never_restarted_labels_annotations_priority(env, mcp, kube, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    name = pod_name(grant["slot"], grant["lease_id"])
    pod = kube.pod(name)
    assert pod["spec"]["restartPolicy"] == "Never"
    assert pod["metadata"]["labels"]["velero.io/exclude-from-backup"] == "true"
    assert pod["metadata"]["annotations"]["descheduler.alpha.kubernetes.io/prefer-no-eviction"] == "true"
    assert pod["spec"]["priorityClassName"] == "emulator-hub-emulator" and pod["spec"]["priority"] == 1000
    assert pod["spec"]["terminationGracePeriodSeconds"] == 5
    kube.kill_container(name)
    await wait_until(lambda: (kube.pod(name) or {}).get("status", {}).get("phase") in ("Failed", None), 30)
    pod = kube.pod(name)
    assert pod is None or pod["status"]["containerStatuses"][0]["restartCount"] == 0


@pytest.mark.real_emulator  # needs the CI runner's disk; the fake runs nowhere near 12 GiB
async def test_emptydir_size_limit_evicts_the_pod(env, mcp, kube, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    name = pod_name(grant["slot"], grant["lease_id"])
    assert kube.pod(name)["spec"]["volumes"][0]["emptyDir"]["sizeLimit"] == "12Gi"
    # Fill /avd past 12Gi; the kubelet evicts within its housekeeping interval.
    kube.exec(name, "sh", "-c", "dd if=/dev/zero of=/avd/fill bs=4M count=3200 status=none || true", timeout=1200)

    async def ended():
        return await _hb_error(mcp, grant["lease_id"])

    msg = await wait_until(ended, 300, interval=5, what="eviction to end the lease")
    assert "(lost)" in msg


async def test_release_removes_the_pod_within_ten_seconds(env, mcp, kube, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    name = pod_name(grant["slot"], grant["lease_id"])
    started = time.monotonic()
    await mcp.call("release", lease_id=grant["lease_id"])
    await wait_until(lambda: kube.pod(name) is None, 15, interval=0.25)
    assert time.monotonic() - started < 10


# ================================================================== network (ENG-341)


@pytest.mark.android
async def test_each_slot_ip_routes_to_its_own_device(env, mcp, kube, adb, profile, holder):
    grants = [await acquire_leased(mcp, env, profile, f"{holder}-{i}") for i in range(len(env.slot_ips))]
    for g in grants:
        name = pod_name(g["slot"], g["lease_id"])
        if env.real:
            adb.connect(g["adb"])
            adb.shell(g["adb"], f"echo {name} > /data/local/tmp/who")
        else:
            assert tcp_banner(env.slot_ips[g["slot"]], 5555) == name
    if env.real:
        for g in grants:
            assert adb.shell(g["adb"], "cat /data/local/tmp/who").strip() == pod_name(g["slot"], g["lease_id"])


async def test_grpc_is_reachable_from_the_hub_only(env, mcp, kube, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    ip = kube.pod(pod_name(grant["slot"], grant["lease_id"]))["status"]["podIP"]
    for ns in ("e2e-client", "e2e-scratch"):
        out = kube.exec("toolbox", "sh", "-c", f"nc -z -w 3 {ip} 8554 && echo open || echo closed", ns=ns)
        assert out.strip() == "closed", ns
    # adb stays reachable in-cluster.
    out = kube.exec("toolbox", "sh", "-c", f"nc -z -w 3 {ip} 5557 && echo open || echo closed", ns="e2e-scratch")
    assert out.strip() == "open"
    hub = kube.hub_pod()["metadata"]["name"]
    out = kube.exec(hub, "python", "-c", f"import socket;socket.create_connection(('{ip}',8554),3);print('open')")
    assert out.strip() == "open"
    # And it is not exposed outside the cluster: the slot Service carries adb only.
    svc = kube.json("-n", NS, "get", "svc", f"slot-{grant['slot']}")
    assert [p["port"] for p in svc["spec"]["ports"]] == [5555]


async def test_machine_port_networkpolicy(env, kube):
    url = "http://emulator-hub.emulator-hub.svc.cluster.local:8081/healthz"
    allowed = kube.exec("toolbox", "sh", "-c", f"curl -s -m 5 {url} || echo blocked", ns="e2e-client")
    denied = kube.exec("toolbox", "sh", "-c", f"curl -s -m 5 {url} || echo blocked", ns="e2e-scratch")
    assert '"ok":true' in allowed.replace(" ", "")
    assert "blocked" in denied


def test_hub_rbac_is_least_privilege(env, kube):
    sa = "system:serviceaccount:emulator-hub:emulator-hub"

    def can(verb, resource, ns=NS):
        resource, _, sub = resource.partition("/")
        args = ["auth", "can-i", verb, resource, "-n", ns, "--as", sa] + ([f"--subresource={sub}"] if sub else [])
        return kube.run(*args, check=False).strip() == "yes"

    for verb in ("create", "delete", "get", "list"):
        assert can(verb, "pods"), verb
    for verb, res in [("watch", "pods"), ("patch", "pods"), ("update", "pods"), ("get", "secrets"),
                      ("list", "secrets"), ("create", "services"), ("create", "pods/exec"), ("get", "nodes"),
                      ("create", "deployments")]:  # fmt: skip
        assert not can(verb, res), (verb, res)
    assert not can("list", "pods", ns="default")
    assert not can("create", "pods", ns="kube-system")


def test_hub_container_identity_and_clean_logs(env, kube, hubctl):
    hub = kube.hub_pod()
    name = hub["metadata"]["name"]
    status = kube.exec(name, "cat", "/proc/1/status")
    assert "10011" in next(line for line in status.splitlines() if line.startswith("Uid:"))
    sc = hub["spec"]["containers"][0]["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False and sc["capabilities"] == {"drop": ["ALL"]}
    assert kube.exec(name, "sh", "-c", "test -w /data && echo ok").strip() == "ok"
    assert "Traceback" not in hubctl.logs()


# ================================================================== metrics (ENG-341)


def test_metrics_are_open_and_prometheus_formatted(env):
    text = metrics_text()
    for name in ("emulator_hub_slots_in_use", "emulator_hub_queue_depth", "emulator_hub_boot_seconds_bucket"):
        assert f"# TYPE {name.removesuffix('_bucket')}" in text
    assert "emulator_hub_leases_ended_total" in text


async def test_slots_in_use_tracks_booting_leased_and_free(env, mcp, hub_env, profile, holder):
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:slow")
    assert metric(metrics_text(), "emulator_hub_slots_in_use") == 0
    grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    assert metric(metrics_text(), "emulator_hub_slots_in_use") == 1  # booting counts
    await mcp.call("release", lease_id=grant["lease_id"])
    assert metric(metrics_text(), "emulator_hub_slots_in_use") == 0


@pytest.mark.android
async def test_boot_seconds_histogram(env, mcp, profile, holder):
    before = metrics_text()
    count0 = metric(before, "emulator_hub_boot_seconds_count")
    sum0 = metric(before, "emulator_hub_boot_seconds_sum")
    started = time.monotonic()
    grant = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=600)
    measured = time.monotonic() - started
    assert grant["state"] == "leased"
    after = metrics_text()
    assert metric(after, "emulator_hub_boot_seconds_count") == count0 + 1
    observed = metric(after, "emulator_hub_boot_seconds_sum") - sum0
    assert abs(observed - measured) <= 5, (observed, measured)
    # A second scrape neither loses nor double-counts it.
    again = metrics_text()
    assert metric(again, "emulator_hub_boot_seconds_count") == count0 + 1


async def test_reaper_reasons_are_counted(env, mcp, kube, hub_env, profile, holder):
    def ended(reason):
        return metric(metrics_text(), "emulator_hub_leases_ended_total", f'reason="{reason}"')

    lost0, expired0, max_age0 = ended("lost"), ended("expired"), ended("max_age")
    g = await acquire_leased(mcp, env, profile, holder)
    kube.delete_pod(pod_name(g["slot"], g["lease_id"]), grace=0)
    await wait_until(lambda: ended("lost") == lost0 + 1, 60, what="lost counted")
    g = await acquire_leased(mcp, env, profile, holder, ttl_minutes=1)
    await wait_until(lambda: ended("expired") == expired0 + 1, 120, interval=2, what="expired counted")
    hub_env(HUB_MAX_AGE_S="20")
    lost0, expired0, max_age0 = ended("lost"), ended("expired"), ended("max_age")  # counters reset on restart
    g = await acquire_leased(mcp, env, profile, holder)
    await wait_until(lambda: ended("max_age") + ended("expired") >= max_age0 + expired0 + 1, 90, interval=2)


@pytest.mark.xfail(
    strict=True,
    reason="emulator_hub_leases_ended_total is only incremented by the reaper; released, forced, boot_failed "
    "and cancelled are never counted (ENG-341)",
)
async def test_every_end_reason_is_counted(env, mcp, hub_env, profile, holder):
    def ended(reason):
        return metric(metrics_text(), "emulator_hub_leases_ended_total", f'reason="{reason}"')

    g = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    released0 = ended("released")
    await mcp.call("release", lease_id=g["lease_id"])
    assert ended("released") == released0 + 1
    g = await mcp.call("acquire", profile=profile, holder=holder, boot_wait_seconds=0)
    forced0 = ended("forced")
    async with ui_client("someone") as ui:
        await ui.delete(f"/api/leases/{g['lease_id']}")
    assert ended("forced") == forced0 + 1


async def test_concurrent_scrapes_and_restart_reset(env, hubctl):
    import httpx

    from tests.e2e.hub import MCP_HOST

    async with httpx.AsyncClient(timeout=30) as c:
        rs = await asyncio.gather(*(c.get(f"http://{MCP_HOST}/metrics") for _ in range(20)))
    assert all(r.status_code == 200 for r in rs)
    # Documented behaviour: counters are process-local and reset on restart.
    hubctl.restart()
    text = metrics_text()
    assert metric(text, "emulator_hub_boot_seconds_count") == 0
    assert metric(text, "emulator_hub_slots_in_use") == 0


# ================================================================== deploy / startup (ENG-334)


async def test_healthz_on_both_ports_without_auth(env, kube):
    import httpx

    from tests.e2e.hub import port_forward

    with port_forward(kube, 8080, 8081) as ports:
        async with httpx.AsyncClient() as c:
            for p in (8080, 8081):
                assert (await c.get(f"http://127.0.0.1:{ports[p]}/healthz")).json() == {"ok": True}


pytest_disruptive = pytest.mark.disruptive


@pytest_disruptive
async def test_first_start_seeds_profiles_and_slots(env, hubctl):
    hubctl.scale(0)
    hubctl.sql("DELETE FROM profiles")
    hubctl.sql("DELETE FROM slots")
    hubctl.scale(1)
    async with ui_client() as ui:
        names = {p["name"] for p in (await ui.get("/api/profiles")).json()}
        st = (await ui.get("/api/status")).json()
    assert names == {"phone", "tablet", "tv"}
    assert [s["state"] for s in st["slots"]] == ["free"] * len(env.slot_ips)
    # Restore the suite's profile.
    import httpx

    from tests.e2e.conftest import E2E_PROFILE
    from tests.e2e.hub import UI_HOST

    with httpx.Client(base_url=f"http://{UI_HOST}", cookies={"e2e_user": "e2e-setup"}) as ui:
        body = {"form_factor": "phone", "system_image": "android-35-google-apis", "device": "medium_phone",
                "ram_mb": 2048 if env.real else 1024, "cores": 2 if env.real else 1}  # fmt: skip
        ui.put(f"/api/profiles/{E2E_PROFILE}", json=body).raise_for_status()


def _run_hub_once(kube, env, extra_env: dict[str, str | None]) -> subprocess.CompletedProcess:
    """Run the hub image as a one-shot Pod with modified env; return its exit + logs."""
    base = {
        "HUB_NAMESPACE": "emulator-hub",
        "HUB_EMULATOR_IMAGE": env.emulator_image,
        "HUB_SLOT_IPS": ",".join(env.slot_ips),
        "HUB_API_TOKEN": "x",
        "HUB_DB_PATH": "/tmp/hub.db",
    }
    merged = {k: v for k, v in {**base, **extra_env}.items() if v is not None}
    name = f"hub-once-{int(time.time() * 1000) % 10**8}"
    overrides = {
        "spec": {
            "serviceAccountName": "emulator-hub",
            "containers": [{"name": name, "image": env.hub_image, "env": [{"name": k, "value": v} for k, v in merged.items()]}],
        }
    }  # fmt: skip
    return subprocess.run(
        ["kubectl", "--context", env.context, "-n", NS, "run", name, "--rm", "-i", "--quiet", "--restart=Never",
         f"--image={env.hub_image}", "--image-pull-policy=IfNotPresent", f"--overrides={json.dumps(overrides)}",
         "--pod-running-timeout=60s"],
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip


@pytest.mark.parametrize("missing", ["HUB_EMULATOR_IMAGE", "HUB_SLOT_IPS", "HUB_API_TOKEN"])
def test_missing_required_config_exits_with_a_readable_error(env, kube, missing):
    res = _run_hub_once(kube, env, {missing: None})
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "validation error for Settings" in out and missing.removeprefix("HUB_").lower() in out


def test_no_rbac_at_startup_exits_before_serving(env, kube):
    overrides_sa = {"serviceAccountName": "default"}
    name = f"hub-norbac-{int(time.time()) % 10**6}"
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": NS},
        "spec": {
            **overrides_sa,
            "restartPolicy": "Never",
            "containers": [{
                "name": "hub", "image": env.hub_image, "imagePullPolicy": "IfNotPresent",
                "env": [{"name": "HUB_EMULATOR_IMAGE", "value": "x"}, {"name": "HUB_SLOT_IPS", "value": "1.2.3.4"},
                        {"name": "HUB_API_TOKEN", "value": "x"}, {"name": "HUB_DB_PATH", "value": "/tmp/h.db"}],
                "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 1},
            }],
        },
    }  # fmt: skip
    kube.apply(manifest)
    try:
        deadline = time.monotonic() + 120
        phase = ""
        while time.monotonic() < deadline:
            p = kube.pod(name)
            phase = p["status"].get("phase", "")
            ready = {c["type"]: c["status"] for c in p["status"].get("conditions", [])}.get("Ready")
            assert ready != "True", "UI port served before reconcile succeeded"
            if phase in ("Failed", "Succeeded"):
                break
            time.sleep(1)
        assert phase == "Failed"
        assert "403" in kube.logs(name) or "forbidden" in kube.logs(name).lower()
    finally:
        kube.delete_pod(name, grace=0)


@pytest_disruptive
async def test_ui_waits_for_reconcile(env, kube, hubctl):
    """Delay reconcile by blocking the API server, and watch the UI port stay
    closed until the machine app finished starting."""
    block = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "e2e-slow-start", "namespace": NS},
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "emulator-hub"}},
            "policyTypes": ["Egress"],
            "egress": [{"ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]}],
        },
    }
    kube.apply(block)
    try:
        kube.run("-n", NS, "rollout", "restart", "deploy/emulator-hub")
        await asyncio.sleep(20)
        pods = [p for p in kube.hub_pods_all() if not p["metadata"].get("deletionTimestamp")]
        newest = max(pods, key=lambda p: p["metadata"]["creationTimestamp"])
        conds = {c["type"]: c["status"] for c in newest["status"].get("conditions", [])}
        assert conds.get("Ready") != "True"
    finally:
        kube.run("-n", NS, "delete", "networkpolicy", "e2e-slow-start", "--ignore-not-found")
    hubctl.wait_rollout(timeout=240)


@pytest_disruptive
async def test_restart_keeps_profiles_and_history(env, mcp, hubctl, holder):
    body = {
        "form_factor": "tv",
        "system_image": "android-36-android-tv",
        "device": "tv_720p",
        "ram_mb": 1024,
        "cores": 1,
    }
    async with ui_client() as ui:
        await ui.put("/api/profiles/keep-me", json=body)
        await ui.put("/api/profiles/delete-me", json=body)
        await ui.delete("/api/profiles/delete-me")
    g = await mcp.call("acquire", profile="keep-me", holder=holder, boot_wait_seconds=0)
    await mcp.call("release", lease_id=g["lease_id"])
    hubctl.restart()
    async with ui_client() as ui:
        names = {p["name"] for p in (await ui.get("/api/profiles")).json()}
        assert "keep-me" in names and "delete-me" not in names
        assert any(r["id"] == g["lease_id"] for r in (await ui.get("/api/leases")).json())
        await ui.delete("/api/profiles/keep-me")


@pytest_disruptive
async def test_growing_slots(env, kube, hub_env, profile, holder):
    import ipaddress

    extra = str(ipaddress.ip_address(env.slot_ips[-1]) + 1)
    n = len(env.slot_ips)
    kube.apply({
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": f"slot-{n}", "namespace": NS, "annotations": {"metallb.io/loadBalancerIPs": extra}},
        "spec": {"type": "LoadBalancer", "selector": {"app.kubernetes.io/name": "emulator", "emulator-hub/slot": str(n)},
                 "ports": [{"name": "adb", "port": 5555, "targetPort": 5557}]},
    })  # fmt: skip
    try:
        hub_env(HUB_SLOT_IPS=",".join([*env.slot_ips, extra]))
        async with McpHolder(env) as m:
            st = await m.call("status")
            assert len(st["slots"]) == n + 1 and st["slots"][n]["state"] == "free"
            # Fill slots 0..n-1 without waiting for their boots; the next lease
            # gets the new slot. Releasing the fillers frees the kvm devices
            # (capacity = original slot count) for the new slot's emulator.
            fillers = [await m.call("acquire", profile=profile, holder=f"{holder}-{i}", boot_wait_seconds=0)
                       for i in range(n)]  # fmt: skip
            assert sorted(f["slot"] for f in fillers) == list(range(n))
            last = await m.call("acquire", profile=profile, holder=f"{holder}-new", boot_wait_seconds=0)
            assert last["slot"] == n and last["adb"] == f"{extra}:5555"
            for f in fillers:
                await m.call("release", lease_id=f["lease_id"])
            g = await _wait_leased(m, env, last)
            if not env.real:
                assert tcp_banner(extra, 5555) == pod_name(g["slot"], g["lease_id"])
            # Release before shrinking back (shrinking under a lease is a known defect).
            await m.call("release", lease_id=g["lease_id"])
    finally:
        kube.run("-n", NS, "delete", "svc", f"slot-{n}", "--ignore-not-found")


async def _wait_leased(m, env, g):
    async def poll():
        nonlocal g
        g = await m.call("heartbeat", lease_id=g["lease_id"])
        return g if g["state"] == "leased" else None

    return await wait_until(poll, env.boot_budget_s, interval=2)


@pytest_disruptive
async def test_shrinking_idle_slots(env, hub_env):
    hub_env(HUB_SLOT_IPS=",".join(env.slot_ips[:-1]))
    async with ui_client() as ui:
        assert len((await ui.get("/api/status")).json()["slots"]) == len(env.slot_ips) - 1


@pytest_disruptive
@pytest.mark.xfail(
    strict=True,
    reason="restarting with fewer HUB_SLOT_IPS while the removed slot is leased strands the lease: "
    "endpoints() raises IndexError and the lease is never ended (ENG-334)",
)
async def test_shrinking_slots_under_an_active_lease(env, mcp, kube, hub_env, profile, holder):
    grants = [await mcp.call("acquire", profile=profile, holder=f"{holder}-{i}", boot_wait_seconds=0)
              for i in range(len(env.slot_ips))]  # fmt: skip
    last = next(g for g in grants if g["slot"] == len(env.slot_ips) - 1)
    hub_env(HUB_SLOT_IPS=",".join(env.slot_ips[:-1]))
    async with McpHolder(env) as m:
        st = await m.call("status")
        assert len(st["slots"]) == len(env.slot_ips) - 1
        with pytest.raises(McpError, match=r"\(lost\)"):
            await m.call("heartbeat", lease_id=last["lease_id"])
    await wait_until(lambda: kube.pod(pod_name(last["slot"], last["lease_id"])) is None, 30)


@pytest_disruptive
async def test_slot_ips_tolerate_whitespace_and_trailing_comma(env, hub_env):
    hub_env(HUB_SLOT_IPS=" " + " , ".join(env.slot_ips) + " ,")
    async with ui_client() as ui:
        assert len((await ui.get("/api/status")).json()["slots"]) == len(env.slot_ips)
    async with McpHolder(env) as m:
        g = await m.call("acquire", profile="e2e", holder="ws", boot_wait_seconds=0)
        assert g["adb"] == f"{env.slot_ips[g['slot']]}:5555"


@pytest_disruptive
async def test_boot_timeout_and_reap_interval_are_honoured(env, mcp, hub_env, profile, holder):
    hub_env(HUB_EMULATOR_IMAGE=f"{env.fake_image}:never-boots", HUB_BOOT_TIMEOUT_S="7", HUB_REAP_INTERVAL_S="1")
    async with McpHolder(env) as m:
        started = time.monotonic()
        with pytest.raises(McpError, match="within 7s"):
            await m.call("acquire", profile=profile, holder=holder, boot_wait_seconds=60)
        assert 6 <= time.monotonic() - started < 20


@pytest_disruptive
async def test_sigterm_leaves_leases_recoverable(env, mcp, kube, hubctl, profile, holder):
    grant = await acquire_leased(mcp, env, profile, holder)
    name = pod_name(grant["slot"], grant["lease_id"])
    uid = kube.pod(name)["metadata"]["uid"]
    old = kube.hub_pod()["metadata"]["name"]
    kube.delete_pod(old, wait=True)  # graceful SIGTERM
    hubctl.wait_rollout()
    logs = kube.run("-n", NS, "logs", old, check=False)  # gone; best effort
    assert "Traceback" not in logs
    assert kube.pod(name)["metadata"]["uid"] == uid
    async with McpHolder(env) as m:
        assert (await m.call("heartbeat", lease_id=grant["lease_id"]))["state"] == "leased"


@pytest_disruptive
def test_rollout_never_runs_two_hubs(env, kube, hubctl):
    dep = kube.json("-n", NS, "get", "deploy", "emulator-hub")
    assert dep["spec"]["strategy"]["type"] == "Recreate" and dep["spec"]["replicas"] == 1
    kube.run("-n", NS, "rollout", "restart", "deploy/emulator-hub")
    deadline = time.monotonic() + 180
    max_running = 0
    while time.monotonic() < deadline:
        running = [p for p in kube.hub_pods_all() if p["status"].get("phase") == "Running"]
        max_running = max(max_running, len(running))
        status = kube.json("-n", NS, "get", "deploy", "emulator-hub")["status"]
        if status.get("updatedReplicas") == 1 and status.get("readyReplicas") == 1 and len(kube.hub_pods_all()) == 1:
            break
        time.sleep(0.5)
    assert max_running == 1
    hubctl.wait_serving()


@pytest_disruptive
async def test_upgrade_from_v0_1_1(env, mcp, kube, hubctl, holder, request):
    old_image = "ghcr.io/noahchalifour/emulator-hub:v0.1.1"
    arch = kube.json("get", "node", env.hub_node)["status"]["nodeInfo"]["architecture"]
    if arch != "amd64":
        pytest.skip(f"{old_image} is published for amd64 only (node is {arch})")
    # The runner has docker but not kind: pull, then import into each node's containerd.
    subprocess.run(["docker", "pull", old_image], check=True, capture_output=True, timeout=600)
    for node in (env.hub_node, f"{env.cluster}-control-plane", env.kvm_node):
        subprocess.run(f"docker save {old_image} | docker exec -i {node} ctr -n k8s.io images import -",
                       shell=True, check=True, capture_output=True, timeout=600)  # fmt: skip
    hubctl.set_image(old_image)
    try:
        body = {"form_factor": "tv", "system_image": "android-36-android-tv", "device": "tv_720p",
                "ram_mb": 1024, "cores": 1}  # fmt: skip
        async with ui_client() as ui:
            assert (await ui.put("/api/profiles/from-v011", json=body)).status_code == 200
        async with McpHolder(env) as m:
            grant = await m.call("acquire", profile="e2e", holder=holder, boot_wait_seconds=0)
            grant = await _wait_leased(m, env, grant)
            ended = await m.call("acquire", profile="e2e", holder=holder, boot_wait_seconds=0)
            await m.call("release", lease_id=ended["lease_id"])
    finally:
        hubctl.set_image(env.hub_image)
    async with McpHolder(env) as m:
        assert (await m.call("heartbeat", lease_id=grant["lease_id"]))["state"] == "leased"
        async with ui_client() as ui:
            assert "from-v011" in {p["name"] for p in (await ui.get("/api/profiles")).json()}
            assert any(r["id"] == ended["lease_id"] for r in (await ui.get("/api/leases")).json())
            assert (await ui.get(f"/api/leases/{grant['lease_id']}/snapshot")).status_code == 200
            await ui.delete("/api/profiles/from-v011")
        await m.call("release", lease_id=grant["lease_id"])


def test_hub_writes_its_db_as_uid_10011(env, kube, hubctl):
    name = kube.hub_pod()["metadata"]["name"]
    out = kube.exec(name, "sh", "-c", "ls -ln /data")
    for line in out.splitlines()[1:]:
        if "hub.db" in line:
            assert line.split()[2] == "10011", line
    assert any("hub.db-wal" in line for line in out.splitlines()), out
