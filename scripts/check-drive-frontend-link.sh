#!/usr/bin/env bash
set -euo pipefail

# Checks the public Drive frontend config and verifies the advertised WebSocket URL.
# With DRIVE_LINK_HEALTH_REPAIR=true, republishes the newest Quick Tunnel URL and retries.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FRONTEND_CONFIG_URL="${FRONTEND_CONFIG_URL:-https://path2v2x.net/config.json}"
DRIVE_CONFIG_URL="${DRIVE_CONFIG_URL:-}"
DRIVE_CONFIG_REQUIRED="${DRIVE_CONFIG_REQUIRED:-true}"
DRIVE_LINK_HEALTH_REPAIR="${DRIVE_LINK_HEALTH_REPAIR:-false}"
PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT="${PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT:-${REPO_ROOT}/scripts/publish-drive-tunnel-config.sh}"
PYTHON_BIN="${PYTHON_BIN:-}"
DRIVE_WS_INSECURE_SSL="${DRIVE_WS_INSECURE_SSL:-false}"
DRIVE_WS_ORIGIN="${DRIVE_WS_ORIGIN:-https://path2v2x.net}"
CHECK_TIMEOUT_SECONDS="${CHECK_TIMEOUT_SECONDS:-15}"
DRIVE_TUNNEL_MODE="${DRIVE_TUNNEL_MODE:-quick}"
PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME:-}"
DRIVE_WS_URL="${DRIVE_WS_URL:-}"

for boolean_name in DRIVE_CONFIG_REQUIRED DRIVE_LINK_HEALTH_REPAIR DRIVE_WS_INSECURE_SSL; do
    boolean_value="${!boolean_name}"
    if [[ "$boolean_value" != "true" && "$boolean_value" != "false" ]]; then
        printf '%s must be true or false (got: %s)\n' "$boolean_name" "$boolean_value" >&2
        exit 2
    fi
done

log() {
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

pick_python() {
    if [[ -n "$PYTHON_BIN" ]]; then
        printf '%s\n' "$PYTHON_BIN"
        return
    fi

    if [[ -x /home/path/venvs/vw-scenario/bin/python ]]; then
        printf '%s\n' /home/path/venvs/vw-scenario/bin/python
        return
    fi

    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return
    fi

    log "ERROR: python3 is not available and PYTHON_BIN is not set."
    exit 2
}

fetch_config() {
    local url="$1"
    local output_file="$2"
    local cache_buster
    local separator
    cache_buster="$(date +%s)"
    separator="?"
    if [[ "$url" == *\?* ]]; then
        separator="&"
    fi

    curl -fsSL --connect-timeout 10 --max-time 20 \
        "${url}${separator}_drive_link_check=${cache_buster}" \
        -o "$output_file"
}

resolve_drive_config_url() {
    local config_file="$1"
    DRIVE_CONFIG_URL="$DRIVE_CONFIG_URL" "$PY" - "$config_file" <<'PY'
import json
import os
import sys

explicit = os.environ.get("DRIVE_CONFIG_URL", "").strip()
if explicit:
    print(explicit)
    raise SystemExit

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    config = json.load(fh)

api_base = str(config.get("apiBaseUrl") or "").rstrip("/")
drive_path = str(config.get("driveConfigPath") or "").strip()
if api_base and drive_path:
    print(f"{api_base}/{drive_path.lstrip('/')}")
PY
}

extract_ws_url() {
    local config_file="$1"
    local overlay_file="$2"
    "$PY" - "$config_file" "$overlay_file" <<'PY'
from datetime import datetime, timezone
import json
import sys

def load(path):
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}

def fresh(config):
    expires_at = config.get("expiresAt")
    if not expires_at:
        return True
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) > datetime.now(timezone.utc)

config = load(sys.argv[1])
overlay = load(sys.argv[2])
url = ""
if fresh(overlay):
    url = overlay.get("cloudflareDriveWsUrl") or ""
url = url or config.get("cloudflareDriveWsUrl") or ""
print(url.strip())
PY
}

validate_overlay() {
    local overlay_file="$1"
    "$PY" - "$overlay_file" <<'PY'
from datetime import datetime, timezone
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        payload = json.load(fh)
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid Drive config JSON: {exc}")

version = payload.get("version")
if not isinstance(version, int) or isinstance(version, bool) or version < 1:
    raise SystemExit("Drive config must have a positive integer version")

expires_at = payload.get("expiresAt")
try:
    parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
except ValueError:
    raise SystemExit("Drive config expiresAt is not ISO-8601")
if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone.utc)
if parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc):
    raise SystemExit("Drive config has expired")

url = payload.get("cloudflareDriveWsUrl")
if not isinstance(url, str) or not url.startswith(("ws://", "wss://")):
    raise SystemExit("Drive config cloudflareDriveWsUrl is invalid")
PY
}

check_ws_url() {
    local ws_url="$1"
    DRIVE_WS_URL="$ws_url" \
    DRIVE_WS_INSECURE_SSL="$DRIVE_WS_INSECURE_SSL" \
    DRIVE_WS_ORIGIN="$DRIVE_WS_ORIGIN" \
    CHECK_TIMEOUT_SECONDS="$CHECK_TIMEOUT_SECONDS" \
    "$PY" <<'PY'
import asyncio
import base64
import hashlib
import os
import socket
import ssl
import sys
from urllib.parse import urlparse

try:
    import websockets
except Exception as exc:
    websockets = None
    websockets_import_error = exc
else:
    websockets_import_error = None

url = os.environ["DRIVE_WS_URL"]
timeout = float(os.environ.get("CHECK_TIMEOUT_SECONDS", "15"))
insecure = os.environ.get("DRIVE_WS_INSECURE_SSL", "true").lower() == "true"
origin = os.environ.get("DRIVE_WS_ORIGIN", "https://path2v2x.net")

ssl_context = None
if url.startswith("wss://") and insecure:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

async def main():
    if websockets is not None:
        connect_kwargs = {
            "open_timeout": timeout,
            "close_timeout": 5,
            "origin": origin,
        }
        # Newer websockets releases reject an explicit ssl=None for wss://
        # URLs. Omit the option for normal certificate verification and only
        # pass a context when insecure mode explicitly requested one.
        if ssl_context is not None:
            connect_kwargs["ssl"] = ssl_context
        async with websockets.connect(url, **connect_kwargs):
            print("WS_OK")
        return

    parsed = urlparse(url)
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
        raise RuntimeError(f"unsupported WebSocket URL: {url}")

    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {parsed.netloc}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Origin: {origin}\r\n"
        "\r\n"
    ).encode("ascii")

    with socket.create_connection((parsed.hostname, port), timeout=timeout) as raw_sock:
        sock = raw_sock
        if parsed.scheme == "wss":
            fallback_ssl_context = ssl.create_default_context()
            if insecure:
                fallback_ssl_context.check_hostname = False
                fallback_ssl_context.verify_mode = ssl.CERT_NONE
            sock = fallback_ssl_context.wrap_socket(raw_sock, server_hostname=parsed.hostname)

        with sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            response = b""
            while b"\r\n\r\n" not in response and len(response) < 8192:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk

    header_text = response.decode("iso-8859-1", errors="replace")
    status_line = header_text.split("\r\n", 1)[0]
    if " 101 " not in status_line:
        raise RuntimeError(f"WebSocket upgrade failed: {status_line}")

    accept = ""
    for line in header_text.split("\r\n")[1:]:
        if line.lower().startswith("sec-websocket-accept:"):
            accept = line.split(":", 1)[1].strip()
            break

    expected = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
    ).decode("ascii")
    if not accept:
        raise RuntimeError("WebSocket upgrade omitted Sec-WebSocket-Accept")
    if accept != expected:
        raise RuntimeError("WebSocket upgrade returned an invalid accept key")

    print(f"WS_OK (stdlib fallback; websockets unavailable: {websockets_import_error})")

try:
    asyncio.run(main())
except Exception as exc:
    print(f"ERROR: WebSocket probe failed for {url}: {type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)
PY
}

run_check_once() {
    local config_file="$1"
    local overlay_file="$2"
    local drive_config_url
    local overlay_error
    local ws_url

    log "Fetching Drive frontend config: ${FRONTEND_CONFIG_URL}"
    fetch_config "$FRONTEND_CONFIG_URL" "$config_file"

    : >"$overlay_file"
    drive_config_url="$(resolve_drive_config_url "$config_file")"
    if [[ -n "$drive_config_url" && "$drive_config_url" != "$FRONTEND_CONFIG_URL" ]]; then
        log "Fetching Drive config overlay: ${drive_config_url}"
        if ! fetch_config "$drive_config_url" "$overlay_file"; then
            if [[ "$DRIVE_CONFIG_REQUIRED" == "true" ]]; then
                log "ERROR: required Drive config overlay is unavailable."
                return 1
            fi
            log "Drive config overlay unavailable; checking the static config fallback."
            : >"$overlay_file"
        elif ! overlay_error="$(validate_overlay "$overlay_file" 2>&1)"; then
            if [[ "$DRIVE_CONFIG_REQUIRED" == "true" ]]; then
                log "ERROR: required Drive config overlay is unusable: ${overlay_error}"
                return 1
            fi
            log "Drive config overlay is unusable (${overlay_error}); checking the static config fallback."
            : >"$overlay_file"
        fi
    elif [[ -n "$drive_config_url" && "$DRIVE_CONFIG_REQUIRED" == "true" ]]; then
        log "ERROR: Drive config overlay resolves to the static frontend config URL."
        return 1
    fi

    ws_url="$(extract_ws_url "$config_file" "$overlay_file")"
    if [[ -z "$ws_url" ]]; then
        log "ERROR: cloudflareDriveWsUrl is empty in ${FRONTEND_CONFIG_URL}"
        return 1
    fi

    log "Checking frontend-advertised Drive WebSocket: ${ws_url}"
    check_ws_url "$ws_url"
}

repair_drive_ws_url() {
    if [[ -n "$DRIVE_WS_URL" ]]; then
        printf '%s\n' "$DRIVE_WS_URL"
        return
    fi
    case "$DRIVE_TUNNEL_MODE" in
        named-config|named-token)
            if [[ -z "$PUBLIC_HOSTNAME" ]]; then
                log "ERROR: named Drive tunnel repair requires PUBLIC_HOSTNAME or DRIVE_WS_URL." >&2
                return 1
            fi
            printf 'wss://%s\n' "$PUBLIC_HOSTNAME"
            ;;
        quick)
            # The publisher derives the process-scoped URL from LOG_FILE.
            printf '\n'
            ;;
        *)
            log "ERROR: unsupported DRIVE_TUNNEL_MODE=$DRIVE_TUNNEL_MODE" >&2
            return 1
            ;;
    esac
}

PY="$(pick_python)"
tmp_config="$(mktemp)"
tmp_overlay="$(mktemp)"
trap 'rm -f "$tmp_config" "$tmp_overlay"' EXIT

if run_check_once "$tmp_config" "$tmp_overlay"; then
    log "Drive frontend link is healthy."
    exit 0
fi

if [[ "$DRIVE_LINK_HEALTH_REPAIR" != "true" ]]; then
    log "Drive frontend link is unhealthy; set DRIVE_LINK_HEALTH_REPAIR=true to republish the latest tunnel URL."
    exit 1
fi

log "Drive frontend link is unhealthy; attempting repair via ${PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT}"
candidate_drive_ws_url="$(repair_drive_ws_url)"
publish_env=(ACTION=publish)
if [[ -n "$candidate_drive_ws_url" ]]; then
    publish_env+=(DRIVE_WS_URL="$candidate_drive_ws_url")
fi
env "${publish_env[@]}" "$PUBLISH_DRIVE_FRONTEND_CONFIG_SCRIPT"

if run_check_once "$tmp_config" "$tmp_overlay"; then
    log "Drive frontend link repair succeeded."
    exit 0
fi

log "ERROR: Drive frontend link repair did not restore WebSocket reachability."
exit 1
