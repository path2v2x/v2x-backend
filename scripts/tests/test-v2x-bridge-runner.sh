#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runner="$repo_root/scripts/test-v2x-bridge.sh"
output="$(mktemp)"
argument_output="$(mktemp)"
override_output="$(mktemp)"
fake_python="$(mktemp)"
fake_marker="$(mktemp)"
hook_file="$(mktemp)"
hook_output="$(mktemp)"
path_output="$(mktemp)"
function_output="$(mktemp)"
python_output="$(mktemp)"
loader_output="$(mktemp)"
source_output="$(mktemp)"
privileged_source_output="$(mktemp)"
fake_bin="$(mktemp -d)"
python_injection="$(mktemp -d)"
attacker_tree="$(mktemp -d)"
rm -f "$fake_marker"
trap 'rm -f "$output" "$argument_output" "$override_output" "$fake_python" "$fake_marker" "$hook_file" "$hook_output" "$path_output" "$function_output" "$python_output" "$loader_output" "$source_output" "$privileged_source_output"; rm -rf "$fake_bin" "$python_injection" "$attacker_tree"' EXIT

if /bin/bash -c 'source "$1"' bash "$runner" >"$source_output" 2>&1; then
  echo "runner accepted ordinary source execution" >&2
  exit 1
fi
if grep -F "[bridge]" "$source_output" >/dev/null; then
  echo "sourced runner started a test lane" >&2
  exit 1
fi

mkdir -p "$attacker_tree/apps/bridge"
cat >"$attacker_tree/apps/bridge/pytest.py" <<EOF
from pathlib import Path
Path('$fake_marker').touch()
print('collected 550 items')
print('550 passed')
print('collected 97 items')
print('97 passed')
EOF
if ATTACKER_TREE="$attacker_tree" RUNNER="$runner" \
  V2X_BRIDGE_RUNNER_DIRECT_EXECUTION_REQUIRED=bypass \
  /bin/bash -p -c '
  pwd() { printf "%s\\n" "$ATTACKER_TREE"; }
  source "$RUNNER"
' >"$privileged_source_output" 2>&1; then
  echo "runner accepted privileged source execution with a forged function" >&2
  exit 1
fi
if [[ -e "$fake_marker" ]]; then
  echo "sourced runner executed attacker-controlled pytest" >&2
  exit 1
fi
if grep -E "collected (550|97) items|550 passed|97 passed|\[bridge\]" \
  "$privileged_source_output" >/dev/null; then
  echo "privileged sourced runner forged or started a test lane" >&2
  exit 1
fi

cat >"$fake_python" <<EOF
#!/usr/bin/env bash
touch '$fake_marker'
exit 99
EOF
chmod +x "$fake_python"

cat >"$hook_file" <<EOF
touch '$fake_marker'
env() { printf 'forged lane output\\n'; }
EOF

if BASH_ENV="$hook_file" ENV="$hook_file" "$runner" >"$hook_output" 2>&1; then
  echo "runner accepted shell startup hooks" >&2
  exit 1
else
  status=$?
fi
if [[ "$status" -ne 2 ]] || [[ -e "$fake_marker" ]]; then
  echo "runner sourced a shell hook or returned an unexpected status" >&2
  exit 1
fi
grep -F "startup hooks are not accepted" "$hook_output" >/dev/null
if grep -F "[bridge]" "$hook_output" >/dev/null; then
  echo "runner started a lane after rejecting shell startup hooks" >&2
  exit 1
fi

cat >"$fake_bin/env" <<EOF
#!/bin/sh
touch '$fake_marker'
printf 'forged lane output\\n'
exit 0
EOF
chmod +x "$fake_bin/env"
if PATH="$fake_bin:/usr/bin:/bin" "$runner" >"$path_output" 2>&1; then
  echo "runner accepted a PATH-controlled env executable" >&2
  exit 1
else
  status=$?
fi
if [[ "$status" -ne 2 ]] || [[ -e "$fake_marker" ]]; then
  echo "runner executed a PATH-controlled env or returned an unexpected status" >&2
  exit 1
fi
grep -F "trusted system location" "$path_output" >/dev/null
if grep -F "[bridge]" "$path_output" >/dev/null; then
  echo "runner started a lane after rejecting hostile PATH" >&2
  exit 1
fi

if /usr/bin/env \
  "BASH_FUNC_env%%=() { touch '$fake_marker'; }" \
  "$runner" >"$function_output" 2>&1; then
  echo "runner accepted an inherited shell function payload" >&2
  exit 1
else
  status=$?
fi
if [[ "$status" -ne 2 ]] || [[ -e "$fake_marker" ]]; then
  echo "runner imported a shell function or returned an unexpected status" >&2
  exit 1
fi
grep -F "inherited shell functions are not accepted" "$function_output" >/dev/null
if grep -F "[bridge]" "$function_output" >/dev/null; then
  echo "runner started a lane after rejecting inherited functions" >&2
  exit 1
fi

cat >"$python_injection/sitecustomize.py" <<EOF
from pathlib import Path
Path('$fake_marker').touch()
EOF
if PYTHONPATH="$python_injection" PYTHONHOME="$python_injection" \
  PYTHONOPTIMIZE=1 PYTHONWARNINGS=ignore \
  "$runner" >"$python_output" 2>&1; then
  echo "runner accepted inherited Python bootstrap controls" >&2
  exit 1
else
  status=$?
fi
if [[ "$status" -ne 2 ]] || [[ -e "$fake_marker" ]]; then
  echo "runner loaded sitecustomize or returned an unexpected status" >&2
  exit 1
fi
grep -F "inherited Python and loader controls are not accepted" \
  "$python_output" >/dev/null
if grep -F "[bridge]" "$python_output" >/dev/null; then
  echo "runner started a lane after rejecting Python bootstrap controls" >&2
  exit 1
fi

if LD_LIBRARY_PATH="$fake_bin" LD_PRELOAD=/v2x/nonexistent/hostile.so \
  "$runner" >"$loader_output" 2>&1; then
  echo "runner accepted inherited loader controls" >&2
  exit 1
else
  status=$?
fi
if [[ "$status" -ne 2 ]]; then
  echo "runner rejected loader controls with unexpected status: $status" >&2
  exit 1
fi
grep -F "inherited Python and loader controls are not accepted" \
  "$loader_output" >/dev/null
if grep -F "[bridge]" "$loader_output" >/dev/null; then
  echo "runner started a lane after rejecting loader controls" >&2
  exit 1
fi

if CARLA_PYTHON="$fake_python" MAP_LIDAR_PYTHON="$fake_python" \
  "$runner" >"$override_output" 2>&1; then
  echo "runner accepted untrusted interpreter overrides" >&2
  exit 1
else
  status=$?
fi
if [[ "$status" -ne 2 ]]; then
  echo "runner rejected interpreter overrides with unexpected status: $status" >&2
  exit 1
fi
grep -F "overrides are not accepted" "$override_output" >/dev/null
if [[ -e "$fake_marker" ]] || grep -F "[bridge]" "$override_output" >/dev/null; then
  echo "runner executed an untrusted interpreter or started a test lane" >&2
  exit 1
fi

if "$runner" tests/test_drive_server.py >"$argument_output" 2>&1; then
  echo "runner accepted a positional pytest selector" >&2
  exit 1
else
  status=$?
fi
if [[ "$status" -ne 2 ]]; then
  echo "runner rejected a selector with unexpected status: $status" >&2
  exit 1
fi
grep -F "does not accept pytest selectors" "$argument_output" >/dev/null
if grep -F "[bridge]" "$argument_output" >/dev/null; then
  echo "runner started a test lane after rejecting a positional selector" >&2
  exit 1
fi

hostile_addopts=(
  --collect-only
  --ignore=tests/test_drive_server.py
  -k
  no_test_can_match_this_expression
)
PYTEST_ADDOPTS="${hostile_addopts[*]}" \
PYTEST_PLUGINS="hostile_plugin_must_not_be_imported" \
  "$runner" | tee "$output"

grep -F "collected 550 items" "$output" >/dev/null
grep -F "550 passed" "$output" >/dev/null
grep -F "collected 97 items" "$output" >/dev/null
grep -F "97 passed" "$output" >/dev/null
if grep -E "collected 0 items|deselected|no tests ran" "$output" >/dev/null; then
  echo "hostile PYTEST_ADDOPTS changed test selection" >&2
  exit 1
fi
