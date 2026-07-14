#!/usr/bin/env bash
set -euo pipefail

if (( $# != 0 )); then
  echo "usage: $0" >&2
  echo "This runner does not accept pytest selectors; both complete lanes are mandatory." >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bridge_dir="$repo_root/apps/bridge"
carla_python="/home/path/V2XCarla/carla-venv-310/bin/python"
map_lidar_python="/home/path/V2XCarla/geospatial-venv/bin/python"

if [[ -v CARLA_PYTHON || -v MAP_LIDAR_PYTHON ]]; then
  echo "CARLA_PYTHON and MAP_LIDAR_PYTHON overrides are not accepted" >&2
  exit 2
fi

for python_bin in "$carla_python" "$map_lidar_python"; do
  if [[ ! -x "$python_bin" ]]; then
    echo "required Python interpreter is not executable: $python_bin" >&2
    exit 1
  fi
done

"$carla_python" - <<'PY'
import importlib.util
import sys
from importlib.metadata import version

if sys.version_info[:2] != (3, 10):
    raise SystemExit(f"CARLA bridge tests require Python 3.10, got {sys.version.split()[0]}")
for module in ("carla", "cv2", "pytest", "pytest_asyncio"):
    if importlib.util.find_spec(module) is None:
        raise SystemExit(f"CARLA bridge test dependency is missing: {module}")
if version("pytest-asyncio") != "1.4.0":
    raise SystemExit("CARLA bridge tests require pytest-asyncio 1.4.0")
PY

"$map_lidar_python" - <<'PY'
import importlib.util
import sys
from importlib.metadata import version

if sys.version_info[:2] != (3, 12):
    raise SystemExit(
        f"map/LiDAR registration tests require Python 3.12, got {sys.version.split()[0]}"
    )
for module in (
    "carla", "cryptography", "laspy", "numpy", "PIL", "pypdf", "pyproj",
    "pytest", "pytest_asyncio", "scipy",
):
    if importlib.util.find_spec(module) is None:
        raise SystemExit(f"map/LiDAR test dependency is missing: {module}")
if version("pytest-asyncio") != "1.4.0":
    raise SystemExit("map/LiDAR tests require pytest-asyncio 1.4.0")
PY

echo "[bridge] CARLA Python 3.10 lane"
(
  cd "$bridge_dir"
  env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS \
    PYTHONWARNINGS=error \
    PYTHONPATH="$bridge_dir" \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    "$carla_python" -m pytest \
      -o addopts= \
      -W error \
      -p pytest_asyncio.plugin \
      tests \
      --ignore=tests/test_register_map_to_lidar.py
)

echo "[bridge] pinned map/LiDAR Python 3.12 lane"
(
  cd "$bridge_dir"
  env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS \
    PYTHONWARNINGS=error \
    PYTHONPATH="$bridge_dir" \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_CORETYPE=Haswell \
    OPENBLAS_NUM_THREADS=1 \
    VECLIB_MAXIMUM_THREADS=1 \
    "$map_lidar_python" -m pytest \
      -o addopts= \
      -W error \
      -p pytest_asyncio.plugin \
      tests/test_register_map_to_lidar.py
)
