#!/usr/bin/env bash
set -euo pipefail

# Backward-compatible Drive-only entry point. The shared publisher defaults to
# ACTION=plan and can also reconcile perception when UPDATE_PERCEPTION=true.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export UPDATE_DRIVE="${UPDATE_DRIVE:-true}"
export UPDATE_PERCEPTION="${UPDATE_PERCEPTION:-false}"
export DRIVE_LOG_FILE="${DRIVE_LOG_FILE:-${LOG_FILE:-/tmp/v2x-cloudflared.log}}"
exec "${SCRIPT_DIR}/publish-amplify-runtime-config.sh" "$@"
