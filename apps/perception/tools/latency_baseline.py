"""Capture a per-stage latency baseline for the live perception pipeline.

Samples the perception /health endpoint and the detections read API for a
bounded window, then reports p50/p95/max for each measurable stage:

  decode   media_timestamp_utc -> decode_received_at (event to decoded frame;
           includes camera->KVS->HLS transport plus NVDEC decode)
  ingest   decode_received_at -> ingested_at (upload batching + API + DynamoDB)
  end_to_end  media_timestamp_utc -> ingested_at

The twin adds a further poll interval (<= 5 s) on top of end_to_end; that
constant is reported, not measured. Only schema-v2 records with
media_time_trusted are counted — untrusted clocks would corrupt the baseline.

Usage:
  latency_baseline.py [--duration 600] [--interval 10]
      [--health-url http://127.0.0.1:8090/health]
      [--api-base https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com]
      [--output baseline.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from datetime import datetime, timezone

TWIN_POLL_INTERVAL_SECONDS = 5.0


def parse_utc(ts: str) -> float:
    """Parse an ISO-8601 UTC timestamp to an epoch float."""
    return (
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
        .astimezone(timezone.utc)
        .timestamp()
    )


def percentile(values, fraction):
    """Nearest-rank percentile; values need not be sorted."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def record_lags(record):
    """Extract (decode_lag, ingest_lag, end_to_end) seconds from one
    detection record, or None if the record lacks a trusted schema-v2 clock.
    """
    if record.get("timestamp_schema_version") != 2:
        return None
    if record.get("media_time_trusted") is not True:
        return None
    try:
        event = parse_utc(record["media_timestamp_utc"])
        decoded = float(record["decode_received_at_epoch"])
        ingested = float(record["ingested_at_epoch"])
    except (KeyError, TypeError, ValueError):
        return None
    decode_lag = decoded - event
    ingest_lag = ingested - decoded
    if decode_lag < 0 or ingest_lag < -1:
        # A negative decode lag means clock disagreement; drop the record
        # rather than let it pull the percentiles down. Ingest gets 1 s of
        # slack because ingested_at is whole-second.
        return None
    return decode_lag, ingest_lag, ingested - event


def summarize(lag_rows, health_decode_ms, sample_count):
    """Build the baseline summary from per-record lag tuples and per-sample
    health decode_latency_ms values."""
    stages = {}
    for name, idx in (("decode", 0), ("ingest", 1), ("end_to_end", 2)):
        values = [row[idx] for row in lag_rows]
        stages[name] = {
            "count": len(values),
            "p50_seconds": percentile(values, 0.50),
            "p95_seconds": percentile(values, 0.95),
            "max_seconds": max(values) if values else None,
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "samples": sample_count,
        "records_used": len(lag_rows),
        "stages": stages,
        "health_decode_latency_ms": {
            "count": len(health_decode_ms),
            "p50": percentile(health_decode_ms, 0.50),
            "p95": percentile(health_decode_ms, 0.95),
            "max": max(health_decode_ms) if health_decode_ms else None,
        },
        "twin_poll_interval_seconds": TWIN_POLL_INTERVAL_SECONDS,
        "note": (
            "twin visibility adds up to twin_poll_interval_seconds on top of "
            "end_to_end"
        ),
    }


def fetch_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument(
        "--health-url", default="http://127.0.0.1:8090/health"
    )
    parser.add_argument(
        "--api-base",
        default="https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com",
    )
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    seen_events = set()
    lag_rows = []
    health_decode_ms = []
    samples = 0
    deadline = time.monotonic() + args.duration

    while time.monotonic() < deadline:
        samples += 1
        try:
            health = fetch_json(args.health_url, timeout=5)
            for camera in health.get("cameras", {}).values():
                latency = camera.get("decode_latency_ms")
                if isinstance(latency, (int, float)) and camera.get("fresh"):
                    health_decode_ms.append(float(latency))
        except Exception as exc:  # noqa: BLE001 - sampling must survive blips
            print(f"health sample failed: {type(exc).__name__}", file=sys.stderr)

        try:
            data = fetch_json(
                f"{args.api_base}/detections/recent?limit=50", timeout=10
            )
            for record in data.get("items", []):
                event_id = record.get("event_id")
                if not event_id or event_id in seen_events:
                    continue
                seen_events.add(event_id)
                lags = record_lags(record)
                if lags is not None:
                    lag_rows.append(lags)
        except Exception as exc:  # noqa: BLE001
            print(
                f"detections sample failed: {type(exc).__name__}",
                file=sys.stderr,
            )

        time.sleep(args.interval)

    summary = summarize(lag_rows, health_decode_ms, samples)
    output = json.dumps(summary, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output + "\n")
    print(output)
    if not lag_rows:
        print("no usable detection records captured", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
