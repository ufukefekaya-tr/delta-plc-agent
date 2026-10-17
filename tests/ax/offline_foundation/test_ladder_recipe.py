from __future__ import annotations

import copy
import json
import random
from pathlib import Path
import unittest

try:
    from jsonschema import Draft202012Validator
except ImportError:  # The locked repository test environment supplies jsonschema.
    Draft202012Validator = None  # type: ignore[assignment]

from axmcp_offline.ladder import (
    LadderRecipeError,
    parse_plcopenxml,
    render_plcopenxml,
    semantic_digest,
    validate_ladder_recipe,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "backends" / "ax"
SCHEMA_PATH = PACKAGE_ROOT / "contracts" / "v2" / "ladder_recipe.schema.json"
VALID_PATH = PACKAGE_ROOT / "contracts" / "v2" / "examples" / "ladder_recipe.valid.json"
INVALID_PATH = PACKAGE_ROOT / "contracts" / "v2" / "examples" / "ladder_recipe.invalid.json"
GOLDEN_RECIPE_PATH = (
    PACKAGE_ROOT / "qualification" / "autonomous" / "fixtures" / "ladder_recipe.golden.json"
)
GOLDEN_XML_PATH = (
    PACKAGE_ROOT
    / "qualification"
    / "autonomous"
    / "fixtures"
    / "ladder_recipe.golden.plcopen.xml"
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def one_network_recipe(
    pou: str,
    output: str,
    *,
    network_id: str = "TEST-NETWORK",
    polarity: str = "NO",
) -> dict:
    return {
        "schema_version": "2",
        "record_type": "LadderRecipe",
        "output_allowlist": [output],
        "project": "SyntheticProject",
        "pou": pou,
        "variables": [
            {
                "name": "Permit",
                "type": "BOOL",
                "direction": "input",
                "comment": "Bounded test permit.",
            },
            {
                "name": output,
                "type": "BOOL",
                "direction": "output",
                "comment": "Single-writer output.",
            },
        ],
        "networks": [
            {
                "id": network_id,
                "order": 1,
                "title": "Single output",
                "comment": "The output follows its declared interlock only.",
                "elements": [
                    {"kind": "contact", "variable": "Permit", "polarity": polarity}
                ],
                "coil": {"variable": output, "mode": "normal"},
            }
        ],
    }


class LadderRecipeSchemaTests(unittest.TestCase):
    @unittest.skipIf(Draft202012Validator is None, "jsonschema is in the locked test venv")
    def test_draft_2020_12_schema_and_examples(self) -> None:
        schema = load_json(SCHEMA_PATH)
        Draft202012Validator.check_schema(schema)
        self.assertFalse(schema["additionalProperties"])
        validator = Draft202012Validator(schema)
        self.assertEqual(list(validator.iter_errors(load_json(VALID_PATH))), [])
        self.assertTrue(list(validator.iter_errors(load_json(INVALID_PATH))))

    def test_semantic_validator_rejects_schema_and_reference_faults(self) -> None:
        valid = load_json(VALID_PATH)
        faults = []

        fault = copy.deepcopy(valid)
        fault["unknown"] = True
        faults.append(fault)

        fault = copy.deepcopy(valid)
        fault["networks"][0]["elements"][0]["variable"] = "Undeclared"
        faults.append(fault)

        fault = copy.deepcopy(valid)
        fault["networks"][0]["coil"]["variable"] = "SafetyPermit"
        faults.append(fault)

        fault = copy.deepcopy(valid)
        fault["pou"] = "Invalid.POU"
        faults.append(fault)

        fault = copy.deepcopy(valid)
        fault["networks"].append(copy.deepcopy(fault["networks"][0]))
        fault["networks"][1]["order"] = 2
        faults.append(fault)

        for index, candidate in enumerate(faults):
            with self.subTest(index=index), self.assertRaises(LadderRecipeError):
                validate_ladder_recipe(candidate)

    def test_exact_project_specific_output_allowlists(self) -> None:
        cases = {
            "MachineSupervisor": ["RunReq", "StopReq", "ResetReq", "RestartRequired"],
            "OutputGroup": [f"Channel_{index}_Run" for index in range(1, 6)],
            "PhysicalOutputs": ["IndicatorOutput", "OutputOne", "OutputTwo", "OutputThree"],
        }
        for pou, outputs in cases.items():
            for output in outputs:
                with self.subTest(pou=pou, output=output):
                    validate_ladder_recipe(one_network_recipe(pou, output))
        denied = one_network_recipe("OutputGroup", "Channel_6_Run")
        denied["output_allowlist"] = ["Channel_1_Run"]
        with self.assertRaises(LadderRecipeError):
            validate_ladder_recipe(denied)


class LadderRendererTests(unittest.TestCase):
    def test_golden_is_deterministic_and_semantically_round_trips(self) -> None:
        recipe = load_json(GOLDEN_RECIPE_PATH)
        rendered = render_plcopenxml(recipe)
        self.assertEqual(rendered, render_plcopenxml(copy.deepcopy(recipe)))
        self.assertEqual(rendered, GOLDEN_XML_PATH.read_text(encoding="utf-8"))
        self.assertEqual(parse_plcopenxml(rendered), recipe)
        self.assertEqual(semantic_digest(parse_plcopenxml(rendered)), semantic_digest(recipe))
        self.assertIn("<LD>", rendered)
        self.assertNotIn("<ST>", rendered)
        input_index = rendered.index("<inputVariables>")
        inout_index = rendered.index("<inOutVariables />", input_index)
        output_index = rendered.index("<outputVariables>", inout_index)
        self.assertLess(input_index, inout_index)
        self.assertLess(inout_index, output_index)

    def test_no_nc_serial_and_parallel_contacts_round_trip(self) -> None:
        recipe = load_json(VALID_PATH)
        rendered = render_plcopenxml(recipe)
        reparsed = parse_plcopenxml(rendered)
        elements = reparsed["networks"][0]["elements"]
        self.assertEqual(elements[0]["polarity"], "NC")
        self.assertEqual(len(elements[1]["paths"]), 2)
        self.assertEqual(parse_plcopenxml(render_plcopenxml(reparsed)), recipe)

    def test_r_trig_ton_tp_are_the_only_supported_calls(self) -> None:
        recipe = load_json(GOLDEN_RECIPE_PATH)
        calls = [
            element["block_type"]
            for element in recipe["networks"][0]["elements"]
            if element["kind"] == "fb_call"
        ]
        self.assertEqual(calls, ["R_TRIG", "TON", "TP"])
        invalid = copy.deepcopy(recipe)
        invalid["networks"][0]["elements"][1]["block_type"] = "TOF"
        with self.assertRaises(LadderRecipeError):
            render_plcopenxml(invalid)

    def test_xml_tampering_and_entities_fail_closed(self) -> None:
        rendered = render_plcopenxml(load_json(GOLDEN_RECIPE_PATH))
        tampered_contact = rendered.replace(
            "<variable>Enable</variable>",
            "<variable>ZeroTransition</variable>",
            1,
        )
        with self.assertRaisesRegex(LadderRecipeError, "contact structure mismatch"):
            parse_plcopenxml(tampered_contact)

        tampered_coil = rendered.replace(">PulseRequest<", ">PulseQ<", 1)
        with self.assertRaisesRegex(LadderRecipeError, "coil structure mismatch"):
            parse_plcopenxml(tampered_coil)

        topology_needle = '<connection refLocalId="13" formalParameter="Q" />'
        topology_prefix, topology_suffix = rendered.rsplit(topology_needle, 1)
        tampered_topology = (
            topology_prefix
            + '<connection refLocalId="9" formalParameter="Q" />'
            + topology_suffix
        )
        with self.assertRaisesRegex(LadderRecipeError, "topology"):
            parse_plcopenxml(tampered_topology)

        unowned_node = rendered.replace(
            "</LD>",
            '<coil localId="999" negated="false" storage="none"><variable>PulseRequest</variable></coil></LD>',
            1,
        )
        with self.assertRaisesRegex(LadderRecipeError, "network reference"):
            parse_plcopenxml(unowned_node)

        unknown_node = rendered.replace("</LD>", "<unknown /></LD>", 1)
        with self.assertRaisesRegex(LadderRecipeError, "unsupported direct LD child"):
            parse_plcopenxml(unknown_node)

        with self.assertRaisesRegex(LadderRecipeError, "DTD"):
            parse_plcopenxml('<!DOCTYPE x [<!ENTITY x "bad">]><x/>')

    def test_bounded_generated_recipes_are_stable(self) -> None:
        randomizer = random.Random(20260901)
        outputs = [f"Channel_{index}_Run" for index in range(1, 6)]
        for iteration in range(25):
            output = randomizer.choice(outputs)
            polarity = randomizer.choice(["NO", "NC"])
            recipe = one_network_recipe(
                "OutputGroup",
                output,
                network_id=f"CHANNEL-CASE{iteration + 1}",
                polarity=polarity,
            )
            rendered = render_plcopenxml(recipe)
            self.assertEqual(parse_plcopenxml(rendered), recipe)
            self.assertEqual(rendered, render_plcopenxml(recipe))

    def test_duplicate_writers_and_noncontiguous_order_fail_closed(self) -> None:
        recipe = one_network_recipe("PhysicalOutputs", "IndicatorOutput")
        second = copy.deepcopy(recipe["networks"][0])
        second["id"] = "TEST-SECOND"
        second["order"] = 2
        recipe["networks"].append(second)
        with self.assertRaisesRegex(LadderRecipeError, "single-writer"):
            validate_ladder_recipe(recipe)

        recipe = one_network_recipe("PhysicalOutputs", "IndicatorOutput")
        recipe["networks"][0]["order"] = 2
        with self.assertRaisesRegex(LadderRecipeError, "contiguous"):
            validate_ladder_recipe(recipe)


if __name__ == "__main__":
    unittest.main()
