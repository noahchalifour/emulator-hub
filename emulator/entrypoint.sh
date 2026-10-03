#!/usr/bin/env bash
# Boots one emulator for one lease. Configured entirely by env (set by the hub):
#   SYSTEM_IMAGE  sdkmanager package, e.g. system-images;android-35;google_apis;x86_64
#   DEVICE        avdmanager device id, e.g. pixel_8
#   RAM_MB, CORES
set -euo pipefail

# containerd >= 2 (and so kind, k3s, recent kubeadm nodes) starts containers
# with RLIMIT_NOFILE=1073741816. The emulator's vCPU threads then hang at boot
# ("detected a hanging thread 'QEMU2 CPU0 thread'") and it aborts; Docker's
# 1048576 boots fine. Clamp it before anything starts.
if (($(ulimit -n) > 1048576)); then
  ulimit -n 1048576
fi

if [[ ! -w /dev/kvm ]]; then
  echo "FATAL: /dev/kvm missing or not writable. The Pod must request squat.ai/kvm and run on an emulator-hub/kvm=true node." >&2
  exit 3
fi

# adb identity: -skip-adb-auth relies on the emulator pushing an adb public
# key into the guest via the ro.boot.qemu.adb.pubkey kernel property, which a
# guest oneshot service (ranchu-adb-setup) writes to /data/misc/adb/adb_keys
# on first boot -- see device/generic/goldfish/init.ranchu.adb.setup.sh in
# AOSP. That property is composed from a key resolved by the emulator's OWN
# embedded copy of the adb-key logic (getPrivateAdbKeyPath() in
# android/emu/adb/interface/.../adbkey.cpp), which looks in
# ConfigDirs::getUserDirectory() -- driven by $ANDROID_EMULATOR_HOME, NOT
# $HOME -- falling back to copying one in from $HOME/.android only if
# nothing is there yet. Meanwhile the "adb" CLIENT processes this entrypoint
# and the emulator shell out to internally (visible in the boot log: "adb -s
# emulator-5554 shell settings put ...") are the external platform-tools
# binary, which resolves its own signing identity via upstream adb's
# adb_get_android_dir_path() (packages/modules/adb/adb_utils.cpp):
# $HOME/.android/adbkey. Two different resolution paths for what is meant to
# be the same identity is exactly the kind of gap -wipe-data plus a
# from-scratch AVD can expose: on a real GitHub Actions/KVM run the
# google_apis/pixel leg authorizes cleanly, but the android-tv leg's own
# boot log shows adbd rejecting even the emulator's own internal calls
# ("device unauthorized. This adb server's $ADB_VENDOR_KEYS is not set"),
# and a prior fix attempt that pre-created the key only at $HOME/.android
# still failed with a "generate_key(...)" re-generation logged partway
# through boot -- consistent with something in that ConfigDirs-driven path
# not finding (or not keeping) the key it expects there. Writing the
# identical key pair to BOTH locations up front removes the ambiguity
# regardless of which lookup a given code path takes, and ADB_VENDOR_KEYS is
# set too so any adb invocation that still looks elsewhere first also finds
# and trusts it. The CI workflow copies the $HOME copy out for the external
# `adb connect` step so it presents the same identity too. This is purely
# additive -- stable, pre-supplied keys instead of ones a given code path
# might generate on its own -- so it should not affect the already-working
# pixel_8/android-35 path.
mkdir -p "$HOME/.android" "$ANDROID_EMULATOR_HOME"
if [[ ! -f "$HOME/.android/adbkey" ]]; then
  adb keygen "$HOME/.android/adbkey"
fi
cp -f "$HOME/.android/adbkey" "$ANDROID_EMULATOR_HOME/adbkey"
cp -f "$HOME/.android/adbkey.pub" "$ANDROID_EMULATOR_HOME/adbkey.pub"
export ADB_VENDOR_KEYS="$HOME/.android/adbkey"

# Start the adb server now, bound to the key above, before the emulator (and
# its own internal "adb -s emulator-5554 ..." calls during boot) can trigger
# an auto-spawned server of its own. google/android-emulator-container-scripts
# does the same thing for the same reason (see install_adb_keys +
# start-server in emu/templates/launch-emulator.sh): a server that
# auto-starts on first use loads whatever key it finds at that moment, and on
# android-tv that has been observed to reload/regenerate independently of the
# key already pushed into the guest, producing "adb: device unauthorized"
# even for the emulator's own internal calls. One long-lived server started
# up front removes that race.
adb start-server

IFS=';' read -r _ _ tag abi <<<"$SYSTEM_IMAGE"
echo no | avdmanager create avd --force --name lease --package "$SYSTEM_IMAGE" \
  --tag "$tag" --abi "$abi" --device "$DEVICE" >/dev/null
# avdmanager defaults hw.keyboard=no, and then the emulator silently drops
# every gRPC sendKey (live-view keys and text): only touch gets through.
sed -i 's/^hw.keyboard=.*/hw.keyboard=yes/' "$ANDROID_AVD_HOME/lease.avd/config.ini"
grep -q '^hw.keyboard=' "$ANDROID_AVD_HOME/lease.avd/config.ini" ||
  echo 'hw.keyboard=yes' >>"$ANDROID_AVD_HOME/lease.avd/config.ini"

# adbd: the emulator binds 127.0.0.1:5555 only; socat publishes it on the Pod
# IP as :5555 via port 5557 -> see Service targetPort. gRPC (-grpc) already
# listens on all interfaces.
socat TCP-LISTEN:5557,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:5555 &

exec emulator -avd lease \
  -no-window -no-audio -no-boot-anim -no-snapshot -wipe-data \
  -gpu swiftshader_indirect -accel on \
  -memory "$RAM_MB" -cores "$CORES" \
  -ports 5554,5555 \
  -grpc 8554 \
  -skip-adb-auth
