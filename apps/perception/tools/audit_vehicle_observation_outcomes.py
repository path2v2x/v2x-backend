#!/usr/bin/env python3
"""Audit one immutable terminal outcome for every vehicle observation.

The command never infers success from a detector box or a persisted object ID.
It verifies the observation ledger, binds every outcome to the exact canonical
ledger row, hashes every retained evidence file, and requires a fail-closed
acceptance report before an observation can be counted as accepted.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile


LEDGER_SCHEMA = "v2x-detection-observation-ledger/v2"
OBSERVATION_SCHEMA = "v2x-detection-observation/v2"
OUTCOME_SCHEMA = "v2x-vehicle-observation-outcome/v1"
REPORT_SCHEMA = "v2x-vehicle-observation-terminal-audit/v1"
ACCEPTANCE_REPORT_SCHEMA = "v2x-vehicle-observation-acceptance/v1"
UNAVAILABILITY_REPORT_SCHEMA = "v2x-vehicle-observation-unavailability/v1"
TERMINAL_STATES = {"accepted", "rejected", "unavailable"}
UNAVAILABLE_REASON = "exact_source_pixels_aged_out"
ACCEPTANCE_GATES = {
    "exact_frame",
    "measured_intrinsics",
    "static_camera",
    "reviewed_contact",
    "localization",
    "identity",
    "temporal_tracking",
    "ue5_visual",
    "cleanup",
}
ACCEPTANCE_ARTIFACTS = {
    "exact_frame",
    "static_calibration",
    "reviewed_contact",
    "identity_track",
    "ue5_replay",
}
ACCEPTANCE_ARTIFACT_SCHEMAS = {
    "exact_frame": "v2x-exact-vehicle-frame-acceptance/v1",
    "static_calibration": "v2x-static-camera-acceptance/v1",
    "reviewed_contact": "v2x-reviewed-vehicle-contact-acceptance/v1",
    "identity_track": "v2x-vehicle-identity-track-acceptance/v1",
    "ue5_replay": "v2x-heldout-ue5-same-car-acceptance/v1",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
REASON_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
MINIMUM_ACCEPTED_FRACTION = 0.80


class OutcomeAuditError(RuntimeError):
    pass


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def canonical_json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def parse_utc(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise OutcomeAuditError(f"{label} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise OutcomeAuditError(f"{label} is invalid") from exc
    return parsed.astimezone(timezone.utc)


def load_json_bytes(path, label):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise OutcomeAuditError(f"{label} is unreadable or invalid") from exc
    if not isinstance(value, dict):
        raise OutcomeAuditError(f"{label} is not an object")
    return path, raw, value


def load_ledger(directory):
    directory = Path(directory).expanduser().resolve()
    manifest_path, manifest_raw, manifest = load_json_bytes(
        directory / "manifest.json", "ledger manifest"
    )
    if manifest.get("schema") != LEDGER_SCHEMA:
        raise OutcomeAuditError("ledger schema is unsupported")
    observations_path = directory / "observations.ndjson"
    try:
        observations_raw = observations_path.read_bytes()
    except OSError as exc:
        raise OutcomeAuditError("ledger observations are unreadable") from exc
    if manifest.get("observations_sha256") != sha256_bytes(observations_raw):
        raise OutcomeAuditError("ledger observations hash does not match manifest")

    observations = {}
    for line_number, line in enumerate(observations_raw.splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OutcomeAuditError(
                f"ledger observation line {line_number} is invalid"
            ) from exc
        event_id = value.get("event_id") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("schema") != OBSERVATION_SCHEMA
            or not isinstance(event_id, str)
            or not event_id
            or value.get("camera_id") not in {"ch1", "ch2", "ch3", "ch4"}
            or event_id in observations
        ):
            raise OutcomeAuditError("ledger observations have invalid or duplicate IDs")
        canonical = canonical_json_bytes(value)
        if line + b"\n" != canonical:
            raise OutcomeAuditError("ledger observations are not canonical JSON")
        observations[event_id] = {
            "value": value,
            "sha256": sha256_bytes(canonical),
        }
    expected_count = (manifest.get("counts") or {}).get("observations")
    if expected_count != len(observations) or not observations:
        raise OutcomeAuditError("ledger observation count does not match manifest")
    return {
        "directory": directory,
        "manifest_path": manifest_path,
        "manifest_sha256": sha256_bytes(manifest_raw),
        "observations_path": observations_path,
        "observations_sha256": sha256_bytes(observations_raw),
        "observations": observations,
    }


def validate_evidence(entries, label):
    if not isinstance(entries, list) or not entries:
        raise OutcomeAuditError(f"{label} has no retained evidence")
    validated = []
    seen = set()
    for entry in entries:
        path_value = entry.get("path") if isinstance(entry, dict) else None
        expected_hash = entry.get("sha256") if isinstance(entry, dict) else None
        if (
            not isinstance(path_value, str)
            or not path_value
            or not isinstance(expected_hash, str)
            or SHA256_RE.fullmatch(expected_hash) is None
        ):
            raise OutcomeAuditError(f"{label} has invalid evidence metadata")
        path = Path(path_value).expanduser().resolve()
        if path in seen:
            raise OutcomeAuditError(f"{label} repeats an evidence file")
        seen.add(path)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise OutcomeAuditError(f"{label} evidence is unreadable") from exc
        actual_hash = sha256_bytes(raw)
        if actual_hash != expected_hash:
            raise OutcomeAuditError(f"{label} evidence hash does not match")
        validated.append({"path": str(path), "sha256": actual_hash})
    return validated


def validate_acceptance_artifact(key, entry, event_id, observation):
    path_value = entry.get("path") if isinstance(entry, dict) else None
    expected_hash = entry.get("sha256") if isinstance(entry, dict) else None
    if (
        not isinstance(path_value, str)
        or not isinstance(expected_hash, str)
        or SHA256_RE.fullmatch(expected_hash) is None
    ):
        raise OutcomeAuditError("acceptance artifact has invalid metadata")
    path, raw, report = load_json_bytes(path_value, f"{key} acceptance artifact")
    if sha256_bytes(raw) != expected_hash:
        raise OutcomeAuditError("acceptance artifact hash does not match")
    gates = report.get("gates")
    if (
        report.get("schema") != ACCEPTANCE_ARTIFACT_SCHEMAS[key]
        or report.get("acceptance_eligible") is not True
        or report.get("acceptance_failures") not in (None, [])
        or not isinstance(gates, dict)
        or not gates
        or not all(value is True for value in gates.values())
    ):
        raise OutcomeAuditError("acceptance artifact does not pass its own gates")
    if key == "static_calibration":
        if report.get("camera_id") != observation["value"]["camera_id"]:
            raise OutcomeAuditError("static calibration artifact is for another camera")
    else:
        event_ids = report.get("event_ids")
        event_bound = report.get("event_id") == event_id or (
            isinstance(event_ids, list)
            and all(isinstance(item, str) and item for item in event_ids)
            and event_id in event_ids
            and len(event_ids) == len(set(event_ids))
        )
        if (
            not event_bound
            or report.get("source_observation_sha256") != observation["sha256"]
        ):
            raise OutcomeAuditError("acceptance artifact is not bound to the observation")
    return {"path": str(path), "sha256": sha256_bytes(raw)}


def validate_acceptance_report(entry, event_id, observation):
    path_value = entry.get("path") if isinstance(entry, dict) else None
    expected_hash = entry.get("sha256") if isinstance(entry, dict) else None
    if (
        not isinstance(path_value, str)
        or not isinstance(expected_hash, str)
        or SHA256_RE.fullmatch(expected_hash) is None
    ):
        raise OutcomeAuditError("accepted outcome has invalid acceptance report metadata")
    path, raw, report = load_json_bytes(path_value, "acceptance report")
    if sha256_bytes(raw) != expected_hash:
        raise OutcomeAuditError("acceptance report hash does not match")
    gates = report.get("gates")
    valid_gates = (
        isinstance(gates, dict)
        and set(gates) == ACCEPTANCE_GATES
        and all(value is True for value in gates.values())
    )
    artifacts = report.get("artifacts")
    if (
        report.get("schema") != ACCEPTANCE_REPORT_SCHEMA
        or
        report.get("acceptance_eligible") is not True
        or report.get("event_id") != event_id
        or report.get("source_observation_sha256") != observation["sha256"]
        or report.get("acceptance_failures") not in (None, [])
        or not isinstance(artifacts, dict)
        or set(artifacts) != ACCEPTANCE_ARTIFACTS
        or not valid_gates
    ):
        raise OutcomeAuditError("accepted outcome report does not pass every bound gate")
    artifact_evidence = {
        key: validate_acceptance_artifact(
            key, artifacts[key], event_id, observation
        )
        for key in sorted(artifacts)
    }
    return {
        "path": str(path),
        "sha256": sha256_bytes(raw),
        "artifacts": artifact_evidence,
    }


def validate_unavailability_report(entry, event_id, observation):
    path_value = entry.get("path") if isinstance(entry, dict) else None
    expected_hash = entry.get("sha256") if isinstance(entry, dict) else None
    if (
        not isinstance(path_value, str)
        or not isinstance(expected_hash, str)
        or SHA256_RE.fullmatch(expected_hash) is None
    ):
        raise OutcomeAuditError("unavailable outcome has invalid report metadata")
    path, raw, report = load_json_bytes(path_value, "unavailability report")
    if sha256_bytes(raw) != expected_hash:
        raise OutcomeAuditError("unavailability report hash does not match")
    expected_stream = f"v2x-backend-cam-{observation['value']['camera_id']}"
    if (
        report.get("schema") != UNAVAILABILITY_REPORT_SCHEMA
        or report.get("event_id") != event_id
        or report.get("source_observation_sha256") != observation["sha256"]
        or report.get("reason_code") != UNAVAILABLE_REASON
        or report.get("requested_media_timestamp_utc")
        != observation["value"].get("media_timestamp_utc")
        or report.get("stream_name") != expected_stream
        or not isinstance(report.get("attempt_count"), int)
        or isinstance(report.get("attempt_count"), bool)
        or report["attempt_count"] < 1
        or not isinstance(report.get("last_attempt_utc"), str)
        or not isinstance(report.get("retention_expired_before_utc"), str)
    ):
        raise OutcomeAuditError("unavailability report is not bound aged-out proof")
    requested = parse_utc(
        report["requested_media_timestamp_utc"], "unavailable requested timestamp"
    )
    expired_before = parse_utc(
        report["retention_expired_before_utc"], "retention expiry boundary"
    )
    last_attempt = parse_utc(report["last_attempt_utc"], "last retrieval attempt")
    if not requested < expired_before <= last_attempt:
        raise OutcomeAuditError("unavailability report does not prove retention expiry")
    return {"path": str(path), "sha256": sha256_bytes(raw)}


def load_outcomes(path, observations):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OutcomeAuditError("outcome ledger is unreadable") from exc
    outcomes = {}
    for line_number, line in enumerate(raw.splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OutcomeAuditError(
                f"outcome line {line_number} is invalid"
            ) from exc
        event_id = value.get("event_id") if isinstance(value, dict) else None
        state = value.get("state") if isinstance(value, dict) else None
        reason = value.get("reason_code") if isinstance(value, dict) else None
        observation = observations.get(event_id)
        if (
            not isinstance(value, dict)
            or value.get("schema") != OUTCOME_SCHEMA
            or not isinstance(event_id, str)
            or event_id in outcomes
        ):
            raise OutcomeAuditError("outcomes have invalid or duplicate event IDs")
        if observation is None:
            raise OutcomeAuditError("outcome references an event outside the ledger")
        if state not in TERMINAL_STATES:
            raise OutcomeAuditError("outcome state is not terminal")
        if value.get("source_observation_sha256") != observation["sha256"]:
            raise OutcomeAuditError("outcome is not bound to its ledger observation")
        if not isinstance(reason, str) or REASON_RE.fullmatch(reason) is None:
            raise OutcomeAuditError("outcome reason code is invalid")
        if state == "accepted" and reason != "all_acceptance_gates_passed":
            raise OutcomeAuditError("accepted outcome has a non-acceptance reason")
        if state == "unavailable" and reason != UNAVAILABLE_REASON:
            raise OutcomeAuditError("unavailable outcome has an unsupported reason")
        if state == "rejected" and reason in {
            "all_acceptance_gates_passed",
            UNAVAILABLE_REASON,
        }:
            raise OutcomeAuditError("rejected outcome has an incompatible reason")
        evidence = validate_evidence(value.get("evidence"), f"outcome {event_id}")
        acceptance_report = None
        unavailability_report = None
        if state == "accepted":
            acceptance_report = validate_acceptance_report(
                value.get("acceptance_report"), event_id, observation
            )
            report_binding = {
                "path": acceptance_report["path"],
                "sha256": acceptance_report["sha256"],
            }
            required_evidence = [
                report_binding,
                *acceptance_report["artifacts"].values(),
            ]
            if any(item not in evidence for item in required_evidence):
                raise OutcomeAuditError(
                    "accepted outcome evidence does not include its complete proof chain"
                )
        elif state == "unavailable":
            unavailability_report = validate_unavailability_report(
                value.get("unavailability_report"), event_id, observation
            )
            if unavailability_report not in evidence:
                raise OutcomeAuditError(
                    "unavailable outcome evidence omits its bound expiry report"
                )
        elif value.get("acceptance_report") is not None:
            raise OutcomeAuditError("non-accepted outcome carries an acceptance report")
        if state != "unavailable" and value.get("unavailability_report") is not None:
            raise OutcomeAuditError("non-unavailable outcome carries an expiry report")
        outcomes[event_id] = {
            "event_id": event_id,
            "camera_id": observation["value"].get("camera_id"),
            "state": state,
            "reason_code": reason,
            "source_observation_sha256": observation["sha256"],
            "evidence": evidence,
            "acceptance_report": acceptance_report,
            "unavailability_report": unavailability_report,
        }
    missing = set(observations) - set(outcomes)
    if missing:
        raise OutcomeAuditError("outcome ledger does not account for every observation")
    return path, raw, outcomes


def build_report(ledger_dir, outcomes_path):
    ledger = load_ledger(ledger_dir)
    outcomes_path, outcomes_raw, outcomes = load_outcomes(
        outcomes_path, ledger["observations"]
    )
    counts = Counter(value["state"] for value in outcomes.values())
    by_camera = {}
    for camera in sorted({value["camera_id"] for value in outcomes.values()}):
        camera_counts = Counter(
            value["state"]
            for value in outcomes.values()
            if value["camera_id"] == camera
        )
        by_camera[camera] = {
            state: camera_counts.get(state, 0) for state in sorted(TERMINAL_STATES)
        }
    recoverable = len(outcomes) - counts.get("unavailable", 0)
    accepted_fraction = counts.get("accepted", 0) / recoverable if recoverable else 0.0
    threshold_passed = (
        math.isfinite(accepted_fraction)
        and accepted_fraction >= MINIMUM_ACCEPTED_FRACTION
    )
    return {
        "schema": REPORT_SCHEMA,
        "acceptance_eligible": threshold_passed,
        "inputs": {
            "ledger_manifest": {
                "path": str(ledger["manifest_path"]),
                "sha256": ledger["manifest_sha256"],
            },
            "ledger_observations": {
                "path": str(ledger["observations_path"]),
                "sha256": ledger["observations_sha256"],
            },
            "outcomes": {
                "path": str(outcomes_path),
                "sha256": sha256_bytes(outcomes_raw),
            },
        },
        "counts": {
            "observations": len(outcomes),
            "recoverable": recoverable,
            **{state: counts.get(state, 0) for state in sorted(TERMINAL_STATES)},
            "by_camera": by_camera,
        },
        "accepted_fraction_of_recoverable": accepted_fraction,
        "gates": {
            "every_observation_has_exactly_one_terminal_outcome": True,
            "all_evidence_hashes_recomputed": True,
            "accepted_reports_are_observation_bound_and_fail_closed": True,
            "unavailable_is_limited_to_aged_out_exact_source_pixels": True,
            "minimum_accepted_fraction": MINIMUM_ACCEPTED_FRACTION,
            "minimum_accepted_fraction_passed": threshold_passed,
        },
        "acceptance_failures": (
            [] if threshold_passed else ["minimum_accepted_fraction_not_met"]
        ),
        "outcomes": [outcomes[event_id] for event_id in sorted(outcomes)],
    }


def write_report_exclusive(path, report):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise OutcomeAuditError("audit output already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(report, indent=2, sort_keys=True).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise OutcomeAuditError("audit output already exists") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--outcomes", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        report = build_report(args.ledger, args.outcomes)
        output = write_report_exclusive(args.output, report)
    except OutcomeAuditError as exc:
        parser.error(str(exc))
    print(json.dumps({
        "output": str(output),
        "acceptance_eligible": report["acceptance_eligible"],
        "counts": report["counts"],
    }, sort_keys=True))
    return 0 if report["acceptance_eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
