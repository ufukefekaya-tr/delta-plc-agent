from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker


REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "backends" / "ax"
V2_ROOT = PACKAGE_ROOT / "contracts" / "v2"
AUTONOMOUS_ROOT = PACKAGE_ROOT / "qualification" / "autonomous"
EXACT_OPERATIONS = (
    "environment.probe",
    "project.tree.read",
    "project.inventory.read",
    "source.read",
    "project.export.native",
    "workspace.create",
    "workspace.status",
    "source.upsert.st",
    "ladder.networks.apply",
    "project.build",
    "artifact.freeze",
    "deployment.prepare",
    "deployment.execute",
    "deployment.reconcile",
    "target.observe.start",
    "target.observe.read",
    "target.observe.stop",
    "job.status",
    "job.cancel",
)


class AutonomousContractTests(unittest.TestCase):
    def test_v2_schemas_are_strict_and_meta_valid(self) -> None:
        schemas = sorted(V2_ROOT.glob("*.schema.json"))
        self.assertGreaterEqual(len(schemas), 7)
        for path in schemas:
            with self.subTest(schema=path.name):
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    schema["$schema"],
                    "https://json-schema.org/draft/2020-12/schema",
                )
                self.assertFalse(schema["additionalProperties"])
                Draft202012Validator.check_schema(schema)

    def test_manifested_instances_pass_and_unknown_fields_fail(self) -> None:
        manifest_path = V2_ROOT / "examples" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for fixture in manifest["fixtures"]:
            schema_path = (manifest_path.parent / fixture["schema"]).resolve()
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            instance = fixture["instance"]
            with self.subTest(schema=schema_path.name):
                self.assertEqual(list(validator.iter_errors(instance)), [])
                unknown = deepcopy(instance)
                unknown["unexpected"] = True
                self.assertTrue(list(validator.iter_errors(unknown)))

    def test_axq_operation_enums_match_default_deny_registry(self) -> None:
        policy = json.loads(
            (AUTONOMOUS_ROOT / "operation-allowlist.json").read_text(
                encoding="utf-8"
            )
        )
        operations = tuple(entry["operation"] for entry in policy["operations"])
        self.assertEqual(operations, EXACT_OPERATIONS)
        self.assertEqual(policy["default_action"], "deny")
        self.assertFalse(
            set(policy["hard_denied"]) & set(operations),
            "hard-denied operation leaked into the public registry",
        )
        for schema_name in (
            "axq_request.schema.json",
            "axq_result.schema.json",
            "evidence_record_v2.schema.json",
            "job_result.schema.json",
        ):
            schema = json.loads((V2_ROOT / schema_name).read_text(encoding="utf-8"))
            self.assertEqual(tuple(schema["$defs"]["operation"]["enum"]), operations)

    def test_deployment_is_fail_closed_before_target_enrollment(self) -> None:
        target = json.loads(
            (AUTONOMOUS_ROOT / "target-enrollment.template.json").read_text(
                encoding="utf-8"
            )
        )
        authority = json.loads(
            (AUTONOMOUS_ROOT / "deployment-authority.template.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(target["status"], "not_enrolled")
        self.assertIsNone(target["fingerprint"])
        self.assertFalse(target["physical_outputs_energized"])
        self.assertEqual(authority["status"], "not_issued")
        self.assertEqual(authority["authority_phrase"], "yükle")
        self.assertIsNone(authority["challenge_nonce"])
        self.assertTrue(authority["single_use"])
        self.assertIsNone(authority["authority_digest"])

    def test_deployment_execute_requires_bound_authorization(self) -> None:
        manifest = json.loads((V2_ROOT / "examples" / "manifest.json").read_text(encoding="utf-8"))
        request = deepcopy(manifest["fixtures"][0]["instance"])
        request["operation"] = "deployment.execute"
        schema = json.loads((V2_ROOT / "axq_request.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        self.assertTrue(list(validator.iter_errors(request)))

        plan_fixture = next(
            item["instance"]
            for item in manifest["fixtures"]
            if item["schema"] == "../deployment_plan.schema.json"
        )
        plan_schema = json.loads((V2_ROOT / "deployment_plan.schema.json").read_text(encoding="utf-8"))
        authorized = deepcopy(plan_fixture)
        authorized["state"] = "authorized"
        self.assertTrue(list(Draft202012Validator(plan_schema).iter_errors(authorized)))
        self.assertEqual(plan_fixture["valid_for_seconds"], 600)
        self.assertTrue(plan_schema["x-invariants"])

    def test_observation_profile_is_bounded_and_read_only(self) -> None:
        profile = json.loads(
            (AUTONOMOUS_ROOT / "observation-profile.universal-plc.json").read_text(
                encoding="utf-8"
            )
        )
        schema = json.loads(
            (V2_ROOT / "observation_profile.schema.json").read_text(encoding="utf-8")
        )
        errors = list(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
                profile
            )
        )
        self.assertEqual(errors, [])
        self.assertLessEqual(profile["duration_seconds"], 300)
        self.assertGreaterEqual(profile["interval_ms"], 100)
        self.assertTrue(
            all(
                set(expression) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.")
                for expression in profile["expressions"]
            )
        )


if __name__ == "__main__":
    unittest.main()
