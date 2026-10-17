from __future__ import annotations

import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker


REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = (
    REPO_ROOT / "backends" / "ax" / "contracts"
)
EXAMPLE_ROOT = CONTRACT_ROOT / "examples"


class StrictContractSchemaTests(unittest.TestCase):
    def test_exactly_three_draft_2020_12_schemas_are_meta_valid(self) -> None:
        schemas = sorted(CONTRACT_ROOT.glob("*.schema.json"))
        self.assertEqual(
            [path.name for path in schemas],
            [
                "capability_result.schema.json",
                "environment_identity.schema.json",
                "evidence_record.schema.json",
            ],
        )
        for path in schemas:
            with self.subTest(schema=path.name):
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    schema["$schema"],
                    "https://json-schema.org/draft/2020-12/schema",
                )
                self.assertFalse(schema["additionalProperties"])
                Draft202012Validator.check_schema(schema)

    def test_manifested_valid_and_invalid_examples_have_expected_outcome(self) -> None:
        manifest = json.loads(
            (EXAMPLE_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        accepted = 0
        rejected = 0
        for entry in manifest["fixtures"]:
            schema = json.loads(
                (EXAMPLE_ROOT / entry["schema"]).resolve().read_text(encoding="utf-8")
            )
            instance = json.loads(
                (EXAMPLE_ROOT / entry["fixture"]).read_text(encoding="utf-8")
            )
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            errors = list(validator.iter_errors(instance))
            with self.subTest(fixture=entry["fixture"]):
                if entry["expected"] == "accept":
                    self.assertEqual(errors, [])
                    accepted += 1
                else:
                    self.assertTrue(errors)
                    rejected += 1
        self.assertEqual((accepted, rejected), (3, 18))

    def test_contract_versions_and_unknown_fields_are_fail_closed(self) -> None:
        for schema_path in CONTRACT_ROOT.glob("*.schema.json"):
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            self.assertEqual(schema["properties"]["schema_version"], {"const": "1"})
            self.assertFalse(schema["additionalProperties"])

    def test_recorded_chain_evidence_is_a_strict_evidence_record(self) -> None:
        schema = json.loads(
            (CONTRACT_ROOT / "evidence_record.schema.json").read_text(encoding="utf-8")
        )
        fixture = json.loads(
            (
                REPO_ROOT
                / "backends" / "ax"
                / "qualification"
                / "f00"
                / "fixtures"
                / "evidence.valid.json"
            ).read_text(encoding="utf-8")
        )
        errors = list(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
                fixture
            )
        )
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
