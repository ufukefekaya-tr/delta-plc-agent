"""A8 private-runner mistakes become offline evidence regressions, not live claims."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import asyncio
import unittest
import tempfile
from pathlib import Path
from mcp import Client
from axmcp_offline.operator_evidence import profile, check_evidence, FAULTS, PROFILE_ID
from axmcp_offline.security import ProtocolViolation
from axmcp_offline.mcp_server import create_server
from axmcp_offline.service import OfflineService
from tests.ax.offline_foundation.test_autonomous_host import _activate_root


def row(value):
    return {"value": value, "quality": "Good"}


def sample(kind):
    values = {key: False for key in FAULTS}
    if kind == "watch":
        values.update(automatic=False, motion_standstill=True, fresh=[True]*6,
                      run_requests=[False]*6, run_acks=[False]*6,
                      running=[False]*6, frequency_hz=[0.0]*6)
    elif kind == "coefficient":
        values.update(channel="channel_a", requested=1.125, draft=1.125, active=1.125,
                      other_before=1.0, other_after=1.0, ack_before=3, ack_after=4,
                      result=1, pulse_released=True)
    else:
        values.update(participants_at_admission=15, participants=15, skipped=48,
                      fresh=[True]*4+[False]*2, ack_before=3, ack_after=4,
                      busy=False, result=1, pulse_released=True, valid=[True]*8+[False]*4,
                      requested=[2.0]*12, readback=[2.0]*12)
    return {"captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "target_fingerprint": "sha256:"+"a"*64,
            "expected_fingerprint": "sha256:"+"a"*64, "marker": 1,
            "fields": {key: row(value) for key, value in values.items()}}


class OperatorEvidenceTests(unittest.TestCase):
    def test_profile_keeps_legacy_symbol_roles_and_distinct_stop(self):
        data = profile()
        self.assertTrue(data["jog"]["channel_a"].endswith("channel_a"))
        self.assertTrue(data["jog"]["channel_b"].endswith("channel_b"))
        self.assertEqual(data["stop_meanings"]["target.observe.stop"], "end_watch_only")
        self.assertFalse(data["runtime_dispatch"])
        self.assertFalse(data["marker_is_binary_identity"])

    def test_positive_evidence_never_claims_live_acceptance(self):
        for kind in ("watch", "coefficient", "ramps"):
            result = check_evidence(PROFILE_ID, kind, sample(kind))
            self.assertTrue(result["evidence_matches"], result)
            self.assertFalse(result["live_ax_verified"])
            self.assertFalse(result["network_contacted"])
            self.assertFalse(result["scan_atomic"])

    def test_active_fault_with_zero_numeric_code_is_not_clean(self):
        for kind in ("watch", "coefficient", "ramps"):
            for fault in FAULTS:
                evidence = sample(kind)
                evidence["fields"][fault] = row(True)
                evidence["fields"]["aggregated_motion_code"] = row(0)
                result = check_evidence(PROFILE_ID, kind, evidence)
                self.assertFalse(result["evidence_matches"])
                self.assertIn(fault, result["details"]["active_faults"])

    def test_missing_null_bad_and_non_boolean_fault_fail(self):
        for value in (None, 0, "FALSE", []):
            evidence = sample("watch")
            evidence["fields"]["motion_fault"] = row(value)
            self.assertFalse(check_evidence(PROFILE_ID, "watch", evidence)["evidence_matches"])
        for mutation in ("missing", "Bad"):
            evidence = sample("watch")
            if mutation == "missing":
                del evidence["fields"]["motion_fault"]
            else:
                evidence["fields"]["motion_fault"]["quality"] = mutation
            self.assertFalse(check_evidence(PROFILE_ID, "watch", evidence)["evidence_matches"])

    def test_stale_future_target_marker_and_unknown_envelope_fail(self):
        for seconds in (-31, 10):
            e = sample("watch")
            e["captured_at"] = (datetime.now(UTC)+timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.assertFalse(check_evidence(PROFILE_ID, "watch", e)["evidence_matches"])
        for key, value in (("target_fingerprint", "sha256:"+"b"*64), ("marker", 4), ("marker", True)):
            e = sample("watch"); e[key] = value
            self.assertFalse(check_evidence(PROFILE_ID, "watch", e)["evidence_matches"])
        e = sample("watch"); e["password"] = None
        with self.assertRaises(ProtocolViolation):
            check_evidence(PROFILE_ID, "watch", e)

    def test_stopped_does_not_mean_requests_false_only(self):
        for key, value in (("run_acks", [True]+[False]*5),
                           ("frequency_hz", [2.0]+[0.0]*5),
                           ("running", [True]+[False]*5), ("automatic", True)):
            e = sample("watch"); e["fields"][key] = row(value)
            self.assertFalse(check_evidence(PROFILE_ID, "watch", e)["details"]["stopped_evidence"])

    def test_five_slots_or_stale_optional_slot_cannot_claim_six_stopped(self):
        for values in ([False]*5, [False]*7):
            e = sample("watch"); e["fields"]["run_acks"] = row(values)
            self.assertFalse(check_evidence(PROFILE_ID, "watch", e)["evidence_matches"])
        e = sample("watch"); e["fields"]["fresh"] = row([True]*4+[False]*2)
        self.assertFalse(check_evidence(PROFILE_ID, "watch", e)["details"]["stopped_evidence"])

    def test_new_ack_independent_channel_active_readback_and_release_required(self):
        for key, value in (("ack_after", 3), ("ack_after", True), ("result", True),
                           ("result", 2), ("active", 1.0), ("other_after", 1.125),
                           ("pulse_released", False), ("requested", float("nan")),
                           ("channel", []), ("draft", None)):
            e = sample("coefficient"); e["fields"][key] = row(value)
            self.assertFalse(check_evidence(PROFILE_ID, "coefficient", e)["evidence_matches"], key)

    def test_uint32_ack_wrap_counts_as_new_ack(self):
        e = sample("coefficient")
        e["fields"]["ack_before"] = row(0xFFFFFFFF)
        e["fields"]["ack_after"] = row(0)
        self.assertTrue(check_evidence(PROFILE_ID, "coefficient", e)["evidence_matches"])

    def test_skipped_drives_are_not_reported_as_passed(self):
        result = check_evidence(PROFILE_ID, "ramps", sample("ramps"))
        self.assertEqual(result["details"]["participant_slots"], [1,2,3,4])
        self.assertEqual(result["details"]["skipped_slots"], [5,6])

    def test_ramp_mask_is_frozen_dropout_and_stale_ack_fail(self):
        for key, value in (("participants", 47), ("skipped", 0), ("fresh", [True]*3+[False]*3),
                           ("ack_after", 3), ("busy", True), ("result", 0),
                           ("readback", [2.1]+[2.0]*11), ("valid", [False]*12)):
            e = sample("ramps"); e["fields"][key] = row(value)
            self.assertFalse(check_evidence(PROFILE_ID, "ramps", e)["evidence_matches"], key)

    def test_optional_sixth_participant_can_not_disappear_mid_batch(self):
        e = sample("ramps")
        for key,value in (("participants_at_admission",47),("participants",47),("skipped",16),
                          ("fresh",[True]*6),("valid",[True]*12)):
            e["fields"][key] = row(value)
        self.assertTrue(check_evidence(PROFILE_ID,"ramps",e)["evidence_matches"])
        e["fields"]["fresh"] = row([True]*5+[False])
        self.assertFalse(check_evidence(PROFILE_ID,"ramps",e)["evidence_matches"])

    def test_public_mcp_returns_checked_input_not_live_proof(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                host = _activate_root(Path(tmp))
                service = OfflineService(host)
                async with Client(create_server(service)) as client:
                    result = await client.call_tool("operator.evidence.check", {
                        "profile_id": PROFILE_ID, "kind": "watch", "evidence": sample("watch")})
                    self.assertFalse(result.is_error, result)
                    self.assertFalse(result.structured_content["live_ax_verified"])
        asyncio.run(scenario())

    def test_ramp_release_missing_false_bad_and_unknown_field_fail(self):
        for variant in ("missing", "false", "Bad"):
            e = sample("ramps")
            if variant == "missing":
                del e["fields"]["pulse_released"]
            elif variant == "false":
                e["fields"]["pulse_released"] = row(False)
            else:
                e["fields"]["pulse_released"]["quality"] = "Bad"
            self.assertFalse(check_evidence(PROFILE_ID, "ramps", e)["evidence_matches"])
        for kind in ("watch", "coefficient", "ramps"):
            e = sample(kind); e["fields"]["unknown"] = row(True)
            with self.assertRaises(ProtocolViolation):
                check_evidence(PROFILE_ID, kind, e)

    def test_all_ramp_masks_are_exact_integers(self):
        for key in ("participants_at_admission", "participants", "skipped"):
            for value in (True, 15.0, 48.0):
                e = sample("ramps"); e["fields"][key] = row(value)
                self.assertFalse(check_evidence(PROFILE_ID, "ramps", e)["evidence_matches"])

    def test_optional_aggregate_never_masks_fault_or_bad_code(self):
        for value in (None, 4, True, "0"):
            e = sample("watch"); e["fields"]["aggregated_motion_code"] = row(value)
            self.assertFalse(check_evidence(PROFILE_ID, "watch", e)["evidence_matches"])
