#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
kind delete cluster --name emulator-hub-e2e
rm -rf .state
