from emulator_hub.catalog import DEFAULT_PROFILES
from emulator_hub.models import Profile
from emulator_hub.pods import build_pod, pod_name


def manifest(profile="tv"):
    p = Profile(**next(d for d in DEFAULT_PROFILES if d["name"] == profile))
    return build_pod(namespace="emulator-hub", image="img:1", slot=1, lease_id="abcdef0123456789", profile=p)


def test_pod_is_labelled_for_its_slot_service_and_lease():
    m = manifest()
    assert m["metadata"]["name"] == pod_name(1, "abcdef0123456789") == "emu-slot-1-abcdef01"
    assert m["metadata"]["labels"] == {
        "app.kubernetes.io/name": "emulator",
        "emulator-hub/slot": "1",
        "emulator-hub/lease": "abcdef0123456789",
        "velero.io/exclude-from-backup": "true",
    }


def test_pod_gets_kvm_from_the_device_plugin_not_privilege():
    m = manifest()
    c = m["spec"]["containers"][0]
    assert c["resources"]["requests"]["squat.ai/kvm"] == "1"
    assert c["resources"]["limits"]["squat.ai/kvm"] == "1"
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert "privileged" not in c["securityContext"]
    assert all("hostPath" not in v for v in m["spec"]["volumes"])
    assert m["spec"]["nodeSelector"] == {"emulator-hub/kvm": "true"}


def test_pod_env_selects_the_baked_system_image():
    env = {e["name"]: e["value"] for e in manifest("tv")["spec"]["containers"][0]["env"]}
    assert env == {
        "SYSTEM_IMAGE": "system-images;android-36;android-tv;x86_64",
        "DEVICE": "tv_1080p",
        "RAM_MB": "2048",
        "CORES": "2",
    }


def test_pod_is_never_restarted_and_opts_out_of_descheduling():
    m = manifest()
    assert m["spec"]["restartPolicy"] == "Never"
    assert m["spec"]["automountServiceAccountToken"] is False
    assert m["metadata"]["annotations"]["descheduler.alpha.kubernetes.io/prefer-no-eviction"] == "true"


def test_pod_requests_its_cores_and_has_no_cpu_limit():
    res = manifest("tablet")["spec"]["containers"][0]["resources"]
    assert res["requests"]["cpu"] == "2"
    # tablet: the emulator raises a pixel_tablet guest to 4096 MB; + 2048
    # display overhead, + 1024 headroom for the limit
    assert res["requests"]["memory"] == "6144Mi" and res["limits"]["memory"] == "7168Mi"
    assert "cpu" not in res["limits"]


def test_pod_joins_the_nodes_kvm_group():
    assert manifest()["spec"]["securityContext"]["supplementalGroups"] == [993]


def test_pod_memory_covers_the_images_minimum_guest_ram():
    from emulator_hub.models import Profile
    from emulator_hub.pods import memory_mb

    # The emulator raises an API 35 guest to 2560 MB, and a tablet to 4096 MB.
    phone = Profile("p", "phone", "android-35-google-apis", "pixel_8", 1024, 2)
    assert memory_mb(phone) == (2560 + 1024, 2560 + 1024 + 1024)
    tablet = Profile("s", "tablet", "android-35-google-apis", "pixel_tablet", 1024, 2)
    assert memory_mb(tablet) == (4096 + 2048, 4096 + 2048 + 1024)
    tv = Profile("t", "tv", "android-36-android-tv", "tv_720p", 4096, 2)
    assert memory_mb(tv) == (4096 + 1024, 4096 + 1024 + 1024)
