# Bridge test environments

Run the complete bridge suite with:

```bash
scripts/test-v2x-bridge.sh
```

The runner intentionally has two mandatory lanes because the runtime contracts
are incompatible:

- ordinary bridge tests run with the production CARLA Python 3.10 environment;
- `test_register_map_to_lidar.py` runs with the deterministic Python 3.12
  environment bound by `apps/bridge/tools/map_lidar_toolchain_lock.json`.

Both lanes promote every warning to an error. The runner clears
`PYTEST_ADDOPTS`, overrides configured `addopts`, and accepts no pytest
selectors, so external or positional options cannot weaken collection. Its
`--ignore` in the first lane does not skip coverage: the excluded registration
file is executed in full by the second lane. The second lane also fixes every
thread-control variable required by the tracked lock.

The tracked adversarial check combines hostile `--collect-only`, `--ignore`,
and `-k` values and requires the complete 550-test and 97-test lane totals:

```bash
scripts/tests/test-v2x-bridge-runner.sh
```

On the Path PC the defaults are:

```text
CARLA_PYTHON=/home/path/V2XCarla/carla-venv-310/bin/python
MAP_LIDAR_PYTHON=/home/path/V2XCarla/geospatial-venv/bin/python
```

The interpreters can be overridden with those environment variables, but the
runner rejects the wrong Python version or a missing dependency. At the
2026-07-14 inspection, `/mnt/v2x-ue5/venvs/geospatial` contained the numerical
toolchain but lacked `pypdf`, `pytest`, and `pytest-asyncio`, so it is not a
complete test environment and correctly fails the preflight if selected.
