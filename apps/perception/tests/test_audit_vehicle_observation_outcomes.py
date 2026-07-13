import hashlib
import json
from pathlib import Path

import pytest

from apps.perception.tools import audit_vehicle_observation_outcomes as audit


def canonical(value):
    return audit.canonical_json_bytes(value)


def make_ledger(root, count=5):
    ledger = root / "ledger"
    ledger.mkdir()
    observations = []
    for index in range(count):
        observations.append({
            "schema": audit.OBSERVATION_SCHEMA,
            "event_id": f"event-{index}",
            "camera_id": f"ch{index % 4 + 1}",
            "media_timestamp_utc": "2026-07-11T00:00:00.000Z",
            "acceptance_eligible": False,
        })
    body = b"".join(canonical(value) for value in observations)
    (ledger / "observations.ndjson").write_bytes(body)
    (ledger / "manifest.json").write_text(json.dumps({
        "schema": audit.LEDGER_SCHEMA,
        "observations_sha256": hashlib.sha256(body).hexdigest(),
        "counts": {"observations": count},
    }))
    return ledger, observations


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_outcomes(root, observations, accepted=4):
    rows = []
    for index, observation in enumerate(observations):
        observation_hash = hashlib.sha256(canonical(observation)).hexdigest()
        if index < accepted:
            artifacts = {}
            artifact_evidence = []
            for artifact_name in sorted(audit.ACCEPTANCE_ARTIFACTS):
                artifact = root / f"{artifact_name}-{index}.json"
                artifact_report = {
                    "schema": audit.ACCEPTANCE_ARTIFACT_SCHEMAS[artifact_name],
                    "acceptance_eligible": True,
                    "gates": {"bound": True},
                    "acceptance_failures": [],
                }
                if artifact_name == "static_calibration":
                    artifact_report["camera_id"] = observation["camera_id"]
                else:
                    artifact_report["event_id"] = observation["event_id"]
                    artifact_report["source_observation_sha256"] = observation_hash
                artifact_hash = write_json(artifact, artifact_report)
                binding = {"path": str(artifact), "sha256": artifact_hash}
                artifacts[artifact_name] = binding
                artifact_evidence.append(binding)
            report = root / f"acceptance-{index}.json"
            report_hash = write_json(report, {
                "schema": audit.ACCEPTANCE_REPORT_SCHEMA,
                "event_id": observation["event_id"],
                "source_observation_sha256": observation_hash,
                "acceptance_eligible": True,
                "gates": {key: True for key in sorted(audit.ACCEPTANCE_GATES)},
                "artifacts": artifacts,
                "acceptance_failures": [],
            })
            rows.append({
                "schema": audit.OUTCOME_SCHEMA,
                "event_id": observation["event_id"],
                "source_observation_sha256": observation_hash,
                "state": "accepted",
                "reason_code": "all_acceptance_gates_passed",
                "evidence": [
                    {"path": str(report), "sha256": report_hash},
                    *artifact_evidence,
                ],
                "acceptance_report": {"path": str(report), "sha256": report_hash},
            })
        else:
            evidence = root / f"rejection-{index}.json"
            evidence_hash = write_json(evidence, {"failed": True})
            rows.append({
                "schema": audit.OUTCOME_SCHEMA,
                "event_id": observation["event_id"],
                "source_observation_sha256": observation_hash,
                "state": "rejected",
                "reason_code": "contact_consensus_failed",
                "evidence": [{"path": str(evidence), "sha256": evidence_hash}],
            })
    path = root / "outcomes.ndjson"
    path.write_bytes(b"".join(canonical(value) for value in rows))
    return path, rows


def test_accepts_complete_bound_audit_at_fixed_fraction(tmp_path):
    ledger, observations = make_ledger(tmp_path)
    outcomes, _rows = make_outcomes(tmp_path, observations, accepted=4)
    report = audit.build_report(ledger, outcomes)
    assert report["acceptance_eligible"] is True
    assert report["counts"]["accepted"] == 4
    assert report["counts"]["rejected"] == 1
    assert report["accepted_fraction_of_recoverable"] == 0.8
    assert set(report["counts"]["by_camera"]) == {"ch1", "ch2", "ch3", "ch4"}


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra"])
def test_rejects_non_bijective_outcomes(tmp_path, mutation):
    ledger, observations = make_ledger(tmp_path)
    outcomes, rows = make_outcomes(tmp_path, observations, accepted=4)
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(rows[0])
    else:
        extra = dict(rows[0])
        extra["event_id"] = "outside-ledger"
        rows.append(extra)
    outcomes.write_bytes(b"".join(canonical(value) for value in rows))
    with pytest.raises(audit.OutcomeAuditError):
        audit.build_report(ledger, outcomes)


def test_rejects_tampered_ledger_and_evidence(tmp_path):
    ledger, observations = make_ledger(tmp_path)
    outcomes, rows = make_outcomes(tmp_path, observations, accepted=4)
    evidence = Path(rows[0]["evidence"][0]["path"])
    evidence.write_text("tampered")
    with pytest.raises(audit.OutcomeAuditError, match="evidence hash|report hash"):
        audit.build_report(ledger, outcomes)

    evidence.write_text("restored but differently encoded")
    body = (ledger / "observations.ndjson").read_bytes() + b"{}\n"
    (ledger / "observations.ndjson").write_bytes(body)
    with pytest.raises(audit.OutcomeAuditError, match="ledger observations hash"):
        audit.build_report(ledger, outcomes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("acceptance_eligible", False),
        ("source_observation_sha256", "0" * 64),
        ("gates", {"static": True, "placement": False}),
        ("acceptance_failures", ["failed"]),
    ],
)
def test_rejects_fabricated_or_failing_acceptance_report(tmp_path, field, value):
    ledger, observations = make_ledger(tmp_path)
    outcomes, rows = make_outcomes(tmp_path, observations, accepted=4)
    report_path = Path(rows[0]["acceptance_report"]["path"])
    report = json.loads(report_path.read_text())
    report[field] = value
    report_hash = write_json(report_path, report)
    rows[0]["acceptance_report"]["sha256"] = report_hash
    rows[0]["evidence"][0]["sha256"] = report_hash
    outcomes.write_bytes(b"".join(canonical(item) for item in rows))
    with pytest.raises(audit.OutcomeAuditError, match="does not pass"):
        audit.build_report(ledger, outcomes)


def test_unavailable_is_limited_to_aged_out_exact_pixels(tmp_path):
    ledger, observations = make_ledger(tmp_path, count=1)
    observation_hash = hashlib.sha256(canonical(observations[0])).hexdigest()
    evidence = tmp_path / "expiry.json"
    evidence_hash = write_json(evidence, {
        "schema": audit.UNAVAILABILITY_REPORT_SCHEMA,
        "event_id": observations[0]["event_id"],
        "source_observation_sha256": observation_hash,
        "reason_code": audit.UNAVAILABLE_REASON,
        "requested_media_timestamp_utc": observations[0]["media_timestamp_utc"],
        "stream_name": "v2x-backend-cam-ch1",
        "attempt_count": 2,
        "last_attempt_utc": "2026-07-13T00:00:00.000Z",
        "retention_expired_before_utc": "2026-07-12T00:00:00.000Z",
    })
    outcome = {
        "schema": audit.OUTCOME_SCHEMA,
        "event_id": observations[0]["event_id"],
        "source_observation_sha256": observation_hash,
        "state": "unavailable",
        "reason_code": "permission_denied",
        "evidence": [{"path": str(evidence), "sha256": evidence_hash}],
        "unavailability_report": {"path": str(evidence), "sha256": evidence_hash},
    }
    outcomes = tmp_path / "outcomes.ndjson"
    outcomes.write_bytes(canonical(outcome))
    with pytest.raises(audit.OutcomeAuditError, match="unsupported reason"):
        audit.build_report(ledger, outcomes)
    outcome["reason_code"] = audit.UNAVAILABLE_REASON
    outcomes.write_bytes(canonical(outcome))
    report = audit.build_report(ledger, outcomes)
    assert report["counts"]["unavailable"] == 1
    assert report["acceptance_eligible"] is False


def test_low_accepted_fraction_is_retained_as_failure(tmp_path):
    ledger, observations = make_ledger(tmp_path)
    outcomes, _rows = make_outcomes(tmp_path, observations, accepted=3)
    report = audit.build_report(ledger, outcomes)
    assert report["acceptance_eligible"] is False
    assert report["acceptance_failures"] == ["minimum_accepted_fraction_not_met"]


def test_output_publication_never_replaces_existing_file(tmp_path):
    output = tmp_path / "audit.json"
    output.write_text("existing")
    with pytest.raises(audit.OutcomeAuditError, match="already exists"):
        audit.write_report_exclusive(output, {"schema": audit.REPORT_SCHEMA})
    assert output.read_text() == "existing"


def test_write_report_exclusive_round_trip(tmp_path):
    output = tmp_path / "audit.json"
    audit.write_report_exclusive(output, {"schema": audit.REPORT_SCHEMA})
    assert json.loads(output.read_text())["schema"] == audit.REPORT_SCHEMA
    assert list(tmp_path.glob(".audit.json.tmp-*")) == []
