"""Synthetic six-drive reference contracts and pure evidence checks. No connection or write side effects."""
from __future__ import annotations

import math
from datetime import UTC, datetime
from .security import ProtocolViolation, validate_digest

PROFILE_ID = "example-six-drive-v1"
FAULTS = {name: "Example.Faults." + name for name in
          ("machine_fault", "motion_fault", "print_fault", "drive_fault", "bench_fault")}
COEFFICIENTS = {channel: {key: "Example." + channel + "." + key
                        for key in ("draft", "active", "apply", "ack", "result")}
                for channel in ("channel_a", "channel_b")}
JOG = {channel: "Example.Jog." + channel for channel in
       ("channel_a", "channel_b", "channel_c", "channel_d", "feed_forward", "feed_reverse")}


def profile(profile_id: str = PROFILE_ID) -> dict:
    if profile_id != PROFILE_ID:
        raise ProtocolViolation("unknown operator profile")
    return {
        "profile_id": PROFILE_ID,
        "profile_kind": "SYNTHETIC_REFERENCE_ONLY",
        "revision_markers": [1],
        "marker_is_binary_identity": False,
        "target_enrollment": "REQUIRED_PER_SESSION",
        "network_contacted": False,
        "runtime_dispatch": False,
        "faults": dict(FAULTS),
        "coefficients": {key: dict(value) for key, value in COEFFICIENTS.items()},
        "jog": dict(JOG),
        "drive_count": 6,
        "ramp_participant_masks": [15, 47],
        "stop_meanings": {
            "target.observe.stop": "end_watch_only",
            "operator_stop": "PLC_program_stop_request_and_readback",
            "application_stop": "runtime_stop_distinct_from_operator_stop",
        },
        "rules": [
            "Example symbols are not operational bindings; map in the consumer workspace.",
            "Pin native artifact and live target separately; marker alone is insufficient.",
            "Pulse release belongs in finally before route queries; Stop cancels pending Start.",
            "Each coefficient uses its own NEW ACK, result=1 and active readback.",
            "Reconnect is read-only; never replay Apply/Start automatically.",
            "Ramp participants are frozen at admission; dropout fails; skipped is not passed.",
            "Fault active with code 0 is still a fault. Null/Bad/stale is unknown.",
            "Test fake Home is not physical sensor Home acceptance.",
            "RUN request, ACK, actual Hz and physical motion are separate evidence.",
        ],
    }


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _counter(value):
    return type(value) is int and 0 <= value <= 0xFFFFFFFF


def check_evidence(profile_id: str, kind: str, evidence: dict) -> dict:
    """Check caller-supplied observations, never assert they came from this PLC."""
    profile(profile_id)
    if not isinstance(kind, str) or kind not in {"watch", "coefficient", "ramps"}:
        raise ProtocolViolation("unknown evidence check")
    if not isinstance(evidence, dict) or set(evidence) != {
        "captured_at", "target_fingerprint", "expected_fingerprint", "marker", "fields"
    }:
        raise ProtocolViolation("evidence requires exact identity/time/fields envelope")
    for key in ("target_fingerprint", "expected_fingerprint"):
        validate_digest(evidence[key])
    try:
        captured = datetime.strptime(evidence["captured_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        raise ProtocolViolation("UTC captured_at required") from None
    failures = []
    age = (datetime.now(UTC) - captured).total_seconds()
    if not 0 <= age <= 30:
        failures.append("stale_or_future_observation")
    if evidence["target_fingerprint"] != evidence["expected_fingerprint"]:
        failures.append("target_mismatch")
    if type(evidence["marker"]) is not int or evidence["marker"] not in (1,):
        failures.append("profile_marker_mismatch")
    fields = evidence["fields"]
    if not isinstance(fields, dict) or len(fields) > 64:
        raise ProtocolViolation("bounded fields object required")
    kind_fields = {
        "watch": {"automatic", "motion_standstill", "fresh", "run_requests",
                  "run_acks", "running", "frequency_hz"},
        "coefficient": {"channel", "requested", "draft", "active", "other_before",
                        "other_after", "ack_before", "ack_after", "result", "pulse_released"},
        "ramps": {"participants_at_admission", "participants", "skipped", "fresh",
                  "ack_before", "ack_after", "busy", "result", "valid", "requested",
                  "readback", "pulse_released"},
    }
    if set(fields) - set(FAULTS) - kind_fields[kind] - {"aggregated_motion_code"}:
        raise ProtocolViolation("unknown evidence field")

    def read(key):
        row = fields.get(key)
        if (not isinstance(row, dict) or set(row) != {"value", "quality"}
                or row["quality"] != "Good" or row["value"] is None):
            failures.append("missing_or_bad:" + key)
            return None
        return row["value"]

    # Mandatory for all checks: numeric zero never overrides an active fault.
    active_faults = []
    for key in FAULTS:
        value = read(key)
        if value is True:
            active_faults.append(key)
            failures.append("active_fault:" + key)
        elif value is not False:
            failures.append("unknown_fault:" + key)
    details = {"active_faults": active_faults}
    if "aggregated_motion_code" in fields:
        code = read("aggregated_motion_code")
        if not _counter(code) or code != 0:
            failures.append("nonzero_or_unknown_aggregated_motion_code")
    if kind == "watch":
        auto, still = read("automatic"), read("motion_standstill")
        if type(auto) is not bool or type(still) is not bool:
            failures.append("unknown_motion_state")
        arrays = {key: read(key) for key in
                  ("fresh", "run_requests", "run_acks", "running", "frequency_hz")}
        if any(not isinstance(value, list) or len(value) != 6 for value in arrays.values()):
            failures.append("expected_six_drive_slots")
        else:
            for key, values in arrays.items():
                if key == "frequency_hz":
                    if not all(_number(value) for value in values):
                        failures.append("unknown_drive_frequency")
                elif not all(type(value) is bool for value in values):
                    failures.append("unknown_drive_boolean:" + key)
            if arrays["fresh"] != [True] * 6:
                failures.append("not_all_drive_feedback_fresh")
        details["stopped_evidence"] = (
            not failures and auto is False and still is True
            and all(arrays[key] == [False] * 6 for key in ("run_requests", "run_acks", "running"))
            and all(abs(value) <= 0.01 for value in arrays["frequency_hz"])
        )
        details["physical_motion_verified"] = False
    elif kind == "coefficient":
        values = {key: read(key) for key in (
            "channel", "requested", "draft", "active", "other_before", "other_after",
            "ack_before", "ack_after", "result", "pulse_released")}
        if not isinstance(values["channel"], str) or values["channel"] not in COEFFICIENTS:
            failures.append("unknown_coefficient_channel")
        if not all(_number(values[key]) for key in ("requested", "draft", "active", "other_before", "other_after")):
            failures.append("invalid_coefficient_value")
        elif not (values["requested"] > 0 and values["draft"] == values["requested"] == values["active"]
                  and values["other_before"] == values["other_after"]):
            failures.append("coefficient_readback_mismatch")
        if not (_counter(values["ack_before"]) and _counter(values["ack_after"])
                and values["ack_before"] != values["ack_after"]):
            failures.append("new_channel_ack_missing")
        if type(values["result"]) is not int or values["result"] != 1:
            failures.append("coefficient_not_accepted")
        if values["pulse_released"] is not True:
            failures.append("apply_not_released")
    else:
        values = {key: read(key) for key in (
            "participants_at_admission", "participants", "skipped", "fresh",
            "ack_before", "ack_after", "busy", "result", "valid", "requested", "readback", "pulse_released")}
        mask = values["participants_at_admission"]
        if type(mask) is not int or mask not in (15, 47):
            failures.append("invalid_ramp_participants")
        else:
            slots = [i for i in range(6) if mask & (1 << i)]
            details.update(participant_slots=[i + 1 for i in slots],
                           skipped_slots=[i + 1 for i in range(6) if i not in slots])
            if (type(values["participants"]) is not int or values["participants"] != mask
                    or type(values["skipped"]) is not int or values["skipped"] != (63 ^ mask)):
                failures.append("ramp_participants_changed")
            fresh = values["fresh"]
            if not isinstance(fresh, list) or len(fresh) != 6 or not all(fresh[i] is True for i in slots):
                failures.append("ramp_participant_dropout")
            arrays = [values[key] for key in ("valid", "requested", "readback")]
            if any(not isinstance(v, list) or len(v) != 12 for v in arrays):
                failures.append("expected_twelve_ramp_values")
            else:
                for slot in slots:
                    for index in (2 * slot, 2 * slot + 1):
                        valid, requested, actual = [v[index] for v in arrays]
                        if valid is not True or not _number(requested) or not _number(actual) or requested != actual:
                            failures.append("ramp_readback_mismatch:" + str(index + 1))
        if not (_counter(values["ack_before"]) and _counter(values["ack_after"])
                and values["ack_before"] != values["ack_after"]):
            failures.append("new_ramp_ack_missing")
        if values["busy"] is not False or type(values["result"]) is not int or values["result"] != 1:
            failures.append("ramp_not_complete")
        if values["pulse_released"] is not True:
            failures.append("ramp_request_not_released")
    return {"profile_id": PROFILE_ID, "kind": kind, "evidence_matches": not failures,
            "failures": sorted(set(failures)), "details": details,
            "evidence_class": "CALLER_SUPPLIED_CHECKED", "live_ax_verified": False,
            "network_contacted": False, "scan_atomic": False}
