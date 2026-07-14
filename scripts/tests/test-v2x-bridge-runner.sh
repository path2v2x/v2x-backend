#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runner="$repo_root/scripts/test-v2x-bridge.sh"
output="$(mktemp)"
argument_output="$(mktemp)"
override_output="$(mktemp)"
fake_python="$(mktemp)"
fake_marker="$(mktemp)"
rm -f "$fake_marker"
trap 'rm -f "$output" "$argument_output" "$override_output" "$fake_python" "$fake_marker"' EXIT

cat >"$fake_python" <<EOF
#!/usr/bin/env bash
touch '$fake_marker'
exit 99
EOF
chmod +x "$fake_python"

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
