from __future__ import annotations

import copy
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


TC6_NAMESPACE = "http://www.plcopen.org/xml/tc6_0200"
AX_NAMESPACE = "https://universal-plc.invalid/axmcp/ladder/v1"
XHTML_NAMESPACE = "http://www.w3.org/1999/xhtml"
MAX_XML_BYTES = 1_048_576

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NETWORK_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
_FB_SIGNATURES = {
    "R_TRIG": ({"CLK": "BOOL"}, {"Q": "BOOL"}),
    "TON": ({"IN": "BOOL", "PT": "TIME"}, {"Q": "BOOL", "ET": "TIME"}),
    "TP": ({"IN": "BOOL", "PT": "TIME"}, {"Q": "BOOL", "ET": "TIME"}),
}
_STRUCTURAL_LD_TAGS = {
    _q_name
    for _q_name in (
        "leftPowerRail",
        "rightPowerRail",
        "contact",
        "inVariable",
        "outVariable",
        "block",
        "coil",
    )
}

ET.register_namespace("", TC6_NAMESPACE)
ET.register_namespace("ax", AX_NAMESPACE)


class LadderRecipeError(ValueError):
    """A fail-closed LadderRecipe or PLCopenXML conformance error."""


def _q(name: str) -> str:
    return f"{{{TC6_NAMESPACE}}}{name}"


def _ax(name: str) -> str:
    return f"{{{AX_NAMESPACE}}}{name}"


def _xhtml(name: str) -> str:
    return f"{{{XHTML_NAMESPACE}}}{name}"


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise LadderRecipeError(
            f"{label} key mismatch; missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise LadderRecipeError(f"{label} must be an IEC identifier")
    return value


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LadderRecipeError(f"{label} must be non-empty text")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _semantic_sha(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strict_json(text: str) -> dict[str, Any]:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LadderRecipeError(f"duplicate JSON key in recipe metadata: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise LadderRecipeError(f"non-finite JSON number in recipe metadata: {value}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
    except LadderRecipeError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise LadderRecipeError("invalid embedded LadderRecipe JSON") from exc
    if not isinstance(value, dict):
        raise LadderRecipeError("embedded LadderRecipe must be an object")
    return value


def _walk_elements(elements: Iterable[Mapping[str, Any]]) -> Iterable[Mapping[str, Any]]:
    for element in elements:
        if element["kind"] == "branch":
            for path in element["paths"]:
                yield from _walk_elements(path)
        else:
            yield element


def validate_ladder_recipe(recipe: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the project-bound LadderRecipe v2 and return a defensive copy."""

    if not isinstance(recipe, Mapping):
        raise LadderRecipeError("LadderRecipe must be an object")
    _exact_keys(
        recipe,
        {"schema_version", "record_type", "project", "pou", "output_allowlist", "variables", "networks"},
        "LadderRecipe",
    )
    if recipe["schema_version"] != "2" or recipe["record_type"] != "LadderRecipe":
        raise LadderRecipeError("unsupported LadderRecipe version or record type")
    _identifier(recipe["project"], "project")
    pou = _identifier(recipe["pou"], "pou")
    allowed_outputs = recipe["output_allowlist"]
    if not isinstance(allowed_outputs, list) or not allowed_outputs:
        raise LadderRecipeError("explicit nonempty output_allowlist is required")
    for name in allowed_outputs:
        _identifier(name, "output_allowlist")
    if len(set(allowed_outputs)) != len(allowed_outputs):
        raise LadderRecipeError("duplicate output binding")

    variables = recipe["variables"]
    if not isinstance(variables, list) or not variables:
        raise LadderRecipeError("variables must be a non-empty list")
    declarations: dict[str, tuple[str, str]] = {}
    for index, variable in enumerate(variables):
        if not isinstance(variable, Mapping):
            raise LadderRecipeError(f"variables[{index}] must be an object")
        _exact_keys(variable, {"name", "type", "direction", "comment"}, f"variables[{index}]")
        name = _identifier(variable["name"], f"variables[{index}].name")
        if name in declarations:
            raise LadderRecipeError(f"duplicate variable declaration: {name}")
        data_type = variable["type"]
        direction = variable["direction"]
        if data_type not in {"BOOL", "TIME"}:
            raise LadderRecipeError(f"unsupported Ladder variable type: {data_type!r}")
        if direction not in {"input", "output", "local"}:
            raise LadderRecipeError(f"invalid variable direction: {direction!r}")
        _nonempty(variable["comment"], f"variables[{index}].comment")
        declarations[name] = (data_type, direction)

    networks = recipe["networks"]
    if not isinstance(networks, list) or not 1 <= len(networks) <= 20:
        raise LadderRecipeError("networks must contain between one and twenty entries")
    network_ids: set[str] = set()
    written_variables: set[str] = set()
    block_instances: set[str] = set()
    expected_orders = list(range(1, len(networks) + 1))
    actual_orders: list[int] = []

    for index, network in enumerate(networks):
        label = f"networks[{index}]"
        if not isinstance(network, Mapping):
            raise LadderRecipeError(f"{label} must be an object")
        _exact_keys(network, {"id", "order", "title", "comment", "elements", "coil"}, label)
        network_id = network["id"]
        if not isinstance(network_id, str) or not _NETWORK_ID.fullmatch(network_id):
            raise LadderRecipeError(f"{label}.id must be a stable uppercase network ID")
        if network_id in network_ids:
            raise LadderRecipeError(f"duplicate network ID: {network_id}")
        network_ids.add(network_id)
        order = network["order"]
        if isinstance(order, bool) or not isinstance(order, int):
            raise LadderRecipeError(f"{label}.order must be an integer")
        actual_orders.append(order)
        _nonempty(network["title"], f"{label}.title")
        _nonempty(network["comment"], f"{label}.comment")
        elements = network["elements"]
        if not isinstance(elements, list) or not elements:
            raise LadderRecipeError(f"{label}.elements must be a non-empty list")
        _validate_elements(elements, declarations, pou, f"{label}.elements", written_variables)
        for element in _walk_elements(elements):
            if element["kind"] != "fb_call":
                continue
            instance = element["instance"]
            if instance in block_instances:
                raise LadderRecipeError(f"duplicate function-block instance: {instance}")
            block_instances.add(instance)

        coil = network["coil"]
        if not isinstance(coil, Mapping):
            raise LadderRecipeError(f"{label}.coil must be an object")
        _exact_keys(coil, {"variable", "mode"}, f"{label}.coil")
        coil_variable = _identifier(coil["variable"], f"{label}.coil.variable")
        if coil["mode"] != "normal":
            raise LadderRecipeError("set/reset/inverted coils are outside LadderRecipe v2")
        if coil_variable not in declarations:
            raise LadderRecipeError(f"undeclared coil variable: {coil_variable}")
        if declarations[coil_variable] != ("BOOL", "output"):
            raise LadderRecipeError(f"main coil must reference a BOOL output: {coil_variable}")
        if coil_variable not in allowed_outputs:
            raise LadderRecipeError(f"coil is outside the {pou} output allowlist: {coil_variable}")
        if coil_variable in written_variables:
            raise LadderRecipeError(f"single-writer violation: {coil_variable}")
        written_variables.add(coil_variable)

    if actual_orders != expected_orders:
        raise LadderRecipeError(
            f"network order must be contiguous and list-ordered: expected={expected_orders}, "
            f"actual={actual_orders}"
        )
    return copy.deepcopy(dict(recipe))


def _validate_elements(
    elements: list[Any],
    declarations: Mapping[str, tuple[str, str]],
    pou: str,
    label: str,
    written_variables: set[str],
) -> None:
    for index, element in enumerate(elements):
        element_label = f"{label}[{index}]"
        if not isinstance(element, Mapping):
            raise LadderRecipeError(f"{element_label} must be an object")
        kind = element.get("kind")
        if kind == "contact":
            _exact_keys(element, {"kind", "variable", "polarity"}, element_label)
            variable = _identifier(element["variable"], f"{element_label}.variable")
            if variable not in declarations:
                raise LadderRecipeError(f"undeclared contact variable: {variable}")
            if declarations[variable][0] != "BOOL":
                raise LadderRecipeError(f"contact must reference BOOL: {variable}")
            if element["polarity"] not in {"NO", "NC"}:
                raise LadderRecipeError(f"invalid contact polarity: {element['polarity']!r}")
        elif kind == "branch":
            _exact_keys(element, {"kind", "paths"}, element_label)
            paths = element["paths"]
            if not isinstance(paths, list) or not 2 <= len(paths) <= 8:
                raise LadderRecipeError(f"{element_label}.paths must have two to eight paths")
            for path_index, path in enumerate(paths):
                if not isinstance(path, list) or not path:
                    raise LadderRecipeError(f"{element_label}.paths[{path_index}] must be non-empty")
                for path_element in path:
                    if not isinstance(path_element, Mapping) or path_element.get("kind") != "contact":
                        raise LadderRecipeError("branch paths may contain contacts only in LadderRecipe v2")
                _validate_elements(
                    path,
                    declarations,
                    pou,
                    f"{element_label}.paths[{path_index}]",
                    written_variables,
                )
        elif kind == "fb_call":
            _exact_keys(
                element,
                {"kind", "block_type", "instance", "inputs", "outputs"},
                element_label,
            )
            block_type = element["block_type"]
            if block_type not in _FB_SIGNATURES:
                raise LadderRecipeError(f"unsupported Ladder function block: {block_type!r}")
            _identifier(element["instance"], f"{element_label}.instance")
            expected_inputs, allowed_outputs = _FB_SIGNATURES[block_type]
            actual_inputs = _validate_pin_bindings(
                element["inputs"], declarations, expected_inputs, f"{element_label}.inputs", exact=True
            )
            actual_outputs = _validate_pin_bindings(
                element["outputs"], declarations, allowed_outputs, f"{element_label}.outputs", exact=False
            )
            if "Q" not in actual_outputs:
                raise LadderRecipeError(f"{block_type} output bindings must include Q")
            for variable in actual_outputs.values():
                if declarations[variable][1] != "local":
                    raise LadderRecipeError(f"function-block output must be local: {variable}")
                if variable in written_variables:
                    raise LadderRecipeError(f"single-writer violation: {variable}")
                written_variables.add(variable)
            if set(actual_inputs) != set(expected_inputs):
                raise LadderRecipeError(f"{block_type} input pins must be exactly {sorted(expected_inputs)}")
        else:
            raise LadderRecipeError(f"unsupported Ladder element kind: {kind!r}")


def _validate_pin_bindings(
    bindings: Any,
    declarations: Mapping[str, tuple[str, str]],
    signature: Mapping[str, str],
    label: str,
    *,
    exact: bool,
) -> dict[str, str]:
    if not isinstance(bindings, list) or not bindings:
        raise LadderRecipeError(f"{label} must be a non-empty list")
    result: dict[str, str] = {}
    for index, binding in enumerate(bindings):
        if not isinstance(binding, Mapping):
            raise LadderRecipeError(f"{label}[{index}] must be an object")
        _exact_keys(binding, {"pin", "variable"}, f"{label}[{index}]")
        pin = _identifier(binding["pin"], f"{label}[{index}].pin")
        variable = _identifier(binding["variable"], f"{label}[{index}].variable")
        if pin in result:
            raise LadderRecipeError(f"duplicate function-block pin: {pin}")
        if pin not in signature:
            raise LadderRecipeError(f"unsupported function-block pin: {pin}")
        if variable not in declarations:
            raise LadderRecipeError(f"undeclared function-block variable: {variable}")
        if declarations[variable][0] != signature[pin]:
            raise LadderRecipeError(
                f"{pin} requires {signature[pin]}, got {declarations[variable][0]} for {variable}"
            )
        result[pin] = variable
    if exact and set(result) != set(signature):
        raise LadderRecipeError(f"{label} must bind exactly {sorted(signature)}")
    return result


@dataclass
class _IdAllocator:
    value: int = 0

    def next(self) -> int:
        self.value += 1
        return self.value


Connection = tuple[int, str | None]


def render_plcopenxml(recipe: Mapping[str, Any]) -> str:
    """Render deterministic DIADesigner/SP18 PLCopenXML 2.00 without an ST fallback."""

    normalized = validate_ladder_recipe(recipe)
    allocator = _IdAllocator()
    root = ET.Element(_q("project"))
    ET.SubElement(
        root,
        _q("fileHeader"),
        {
            "companyName": "Universal PLC",
            "productName": "universal-plc-axmcp",
            "productVersion": "0.2.0",
            "creationDateTime": "1970-01-01T00:00:00Z",
        },
    )
    content = ET.SubElement(root, _q("contentHeader"), {"name": normalized["project"]})
    coordinate = ET.SubElement(content, _q("coordinateInfo"))
    for language in ("fbd", "ld", "sfc"):
        language_node = ET.SubElement(coordinate, _q(language))
        ET.SubElement(language_node, _q("scaling"), {"x": "1", "y": "1"})

    types = ET.SubElement(root, _q("types"))
    ET.SubElement(types, _q("dataTypes"))
    pous = ET.SubElement(types, _q("pous"))
    pou = ET.SubElement(pous, _q("pou"), {"name": normalized["pou"], "pouType": "program"})
    interface = ET.SubElement(pou, _q("interface"))
    instances = _function_block_instances(normalized)
    _render_declarations(interface, normalized["variables"], instances)
    body = ET.SubElement(pou, _q("body"))
    ld = ET.SubElement(body, _q("LD"))

    left = ET.SubElement(ld, _q("leftPowerRail"), {"localId": "0"})
    _position(left, 0, 0)
    ET.SubElement(left, _q("connectionPointOut"), {"formalParameter": "none"})

    for network in normalized["networks"]:
        _render_network(ld, network, allocator)

    right = ET.SubElement(ld, _q("rightPowerRail"), {"localId": "2147483646"})
    _position(right, 0, 0)
    ET.SubElement(right, _q("connectionPointIn"))

    add_data = ET.SubElement(pou, _q("addData"))
    plaintext_data = ET.SubElement(
        add_data,
        _q("data"),
        {
            "name": "http://www.3s-software.com/plcopenxml/interfaceasplaintext",
            "handleUnknown": "implementation",
        },
    )
    plaintext = ET.SubElement(plaintext_data, _q("InterfaceAsPlainText"))
    ET.SubElement(plaintext, _xhtml("xhtml")).text = _plaintext_declaration(
        normalized, instances
    )
    data = ET.SubElement(
        add_data,
        _q("data"),
        {"name": "universal-plc.ladder-recipe.v1", "handleUnknown": "implementation"},
    )
    recipe_node = ET.SubElement(data, _ax("recipe"), {"sha256": _semantic_sha(normalized)})
    recipe_node.text = _canonical_json(normalized)

    instances = ET.SubElement(root, _q("instances"))
    ET.SubElement(instances, _q("configurations"))
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def _function_block_instances(recipe: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [
        (element["instance"], element["block_type"])
        for network in recipe["networks"]
        for element in _walk_elements(network["elements"])
        if element["kind"] == "fb_call"
    ]


def _plaintext_declaration(
    recipe: Mapping[str, Any], instances: list[tuple[str, str]]
) -> str:
    lines = [f"PROGRAM {recipe['pou']}"]
    section_names = {"input": "VAR_INPUT", "output": "VAR_OUTPUT", "local": "VAR"}
    for direction in ("input", "output", "local"):
        selected = [item for item in recipe["variables"] if item["direction"] == direction]
        if direction == "local":
            selected = selected + [
                {"name": name, "type": block_type} for name, block_type in instances
            ]
        if not selected:
            continue
        lines.append(section_names[direction])
        lines.extend(f"    {item['name']} : {item['type']};" for item in selected)
        lines.append("END_VAR")
    return "\n".join(lines) + "\n"


def _render_declarations(
    interface: ET.Element,
    variables: list[dict[str, Any]],
    instances: list[tuple[str, str]],
) -> None:
    groups = {"input": "inputVars", "output": "outputVars", "local": "localVars"}
    for direction in ("input", "output", "local"):
        selected = [variable for variable in variables if variable["direction"] == direction]
        instance_variables = instances if direction == "local" else []
        if not selected and not instance_variables:
            continue
        group = ET.SubElement(interface, _q(groups[direction]))
        for variable in selected:
            node = ET.SubElement(group, _q("variable"), {"name": variable["name"]})
            type_node = ET.SubElement(node, _q("type"))
            ET.SubElement(type_node, _q(variable["type"]))
            documentation = ET.SubElement(node, _q("documentation"))
            ET.SubElement(documentation, _xhtml("xhtml")).text = variable["comment"]
        for name, block_type in instance_variables:
            node = ET.SubElement(group, _q("variable"), {"name": name})
            type_node = ET.SubElement(node, _q("type"))
            ET.SubElement(type_node, _q("derived"), {"name": block_type})


def _network_ref(parent: ET.Element, network_id: str) -> None:
    add_data = ET.SubElement(parent, _q("addData"))
    data = ET.SubElement(
        add_data,
        _q("data"),
        {"name": "universal-plc.network-ref.v1", "handleUnknown": "implementation"},
    )
    ET.SubElement(data, _ax("networkRef"), {"id": network_id})


def _position(parent: ET.Element, x: int, y: int) -> None:
    ET.SubElement(parent, _q("position"), {"x": str(x), "y": str(y)})


def _connection_point_in(parent: ET.Element, connections: list[Connection]) -> None:
    point = ET.SubElement(parent, _q("connectionPointIn"))
    ET.SubElement(point, _q("relPosition"), {"x": "0", "y": "0"})
    for local_id, pin in connections:
        attributes = {"refLocalId": str(local_id)}
        if pin:
            attributes["formalParameter"] = pin
        ET.SubElement(point, _q("connection"), attributes)


def _connection_point_out(parent: ET.Element, formal_parameter: str | None = None) -> None:
    attributes = {} if formal_parameter is None else {"formalParameter": formal_parameter}
    point = ET.SubElement(parent, _q("connectionPointOut"), attributes)
    ET.SubElement(point, _q("relPosition"), {"x": "0", "y": "0"})


def _render_network(ld: ET.Element, network: dict[str, Any], allocator: _IdAllocator) -> None:
    network_id = network["id"]
    y = network["order"] * 100
    comment_id = allocator.next()
    comment = ET.SubElement(
        ld,
        _q("comment"),
        {"localId": str(comment_id), "height": "40", "width": "600"},
    )
    _position(comment, 0, y - 50)
    content = ET.SubElement(comment, _q("content"))
    ET.SubElement(content, _xhtml("xhtml")).text = _network_label(network)
    comment_add = ET.SubElement(comment, _q("addData"))
    comment_data = ET.SubElement(
        comment_add,
        _q("data"),
        {"name": "universal-plc.network.v1", "handleUnknown": "implementation"},
    )
    ET.SubElement(
        comment_data,
        _ax("network"),
        {
            "id": network_id,
            "order": str(network["order"]),
            "semanticSha256": _semantic_sha(network),
        },
    )

    vendor = ET.SubElement(ld, _q("vendorElement"), {"localId": str(allocator.next())})
    _position(vendor, 0, 0)
    alternative = ET.SubElement(vendor, _q("alternativeText"))
    ET.SubElement(alternative, _xhtml("xhtml"))
    vendor_add = ET.SubElement(vendor, _q("addData"))
    vendor_data = ET.SubElement(
        vendor_add,
        _q("data"),
        {
            "name": "http://www.3s-software.com/plcopenxml/fbdelementtype",
            "handleUnknown": "implementation",
        },
    )
    ET.SubElement(vendor_data, "ElementType", {"xmlns": ""}).text = "networktitle"

    frontier: list[Connection] = [(0, None)]
    x = 100
    frontier, x = _render_elements(ld, network["elements"], frontier, x, y, network_id, allocator)

    coil_id = allocator.next()
    coil = ET.SubElement(
        ld,
        _q("coil"),
        {"localId": str(coil_id), "negated": "false", "storage": "none"},
    )
    _position(coil, x, y)
    _connection_point_in(coil, frontier)
    _connection_point_out(coil)
    ET.SubElement(coil, _q("variable")).text = network["coil"]["variable"]
    _network_ref(coil, network_id)

def _network_label(network: Mapping[str, Any]) -> str:
    return (
        f"AXMCP[{network['id']}|{network['order']}|{_semantic_sha(network)}] "
        f"{network['title']} | {network['comment']}"
    )


def _render_elements(
    ld: ET.Element,
    elements: list[dict[str, Any]],
    frontier: list[Connection],
    x: int,
    y: int,
    network_id: str,
    allocator: _IdAllocator,
) -> tuple[list[Connection], int]:
    for element in elements:
        if element["kind"] == "contact":
            local_id = allocator.next()
            contact = ET.SubElement(
                ld,
                _q("contact"),
                {
                    "localId": str(local_id),
                    "negated": "true" if element["polarity"] == "NC" else "false",
                },
            )
            _position(contact, x, y)
            _connection_point_in(contact, frontier)
            _connection_point_out(contact)
            ET.SubElement(contact, _q("variable")).text = element["variable"]
            _network_ref(contact, network_id)
            frontier = [(local_id, None)]
            x += 100
        elif element["kind"] == "branch":
            ends: list[Connection] = []
            max_x = x
            for path_index, path in enumerate(element["paths"]):
                path_end, path_x = _render_elements(
                    ld,
                    path,
                    list(frontier),
                    x,
                    y + (path_index * 30),
                    network_id,
                    allocator,
                )
                ends.extend(path_end)
                max_x = max(max_x, path_x)
            frontier = ends
            x = max_x
        else:
            frontier, x = _render_fb_call(
                ld, element, frontier, x, y, network_id, allocator
            )
    return frontier, x


def _render_fb_call(
    ld: ET.Element,
    element: dict[str, Any],
    frontier: list[Connection],
    x: int,
    y: int,
    network_id: str,
    allocator: _IdAllocator,
) -> tuple[list[Connection], int]:
    input_ids: dict[str, int] = {}
    for offset, binding in enumerate(element["inputs"]):
        local_id = allocator.next()
        input_ids[binding["pin"]] = local_id
        in_variable = ET.SubElement(ld, _q("inVariable"), {"localId": str(local_id)})
        _position(in_variable, x, y + 20 + (offset * 20))
        _connection_point_out(in_variable)
        ET.SubElement(in_variable, _q("expression")).text = binding["variable"]
        _network_ref(in_variable, network_id)

    block_id = allocator.next()
    block = ET.SubElement(
        ld,
        _q("block"),
        {
            "localId": str(block_id),
            "typeName": element["block_type"],
            "instanceName": element["instance"],
        },
    )
    _position(block, x + 100, y)
    inputs = ET.SubElement(block, _q("inputVariables"))
    enable = ET.SubElement(inputs, _q("variable"), {"formalParameter": "EN"})
    _connection_point_in(enable, frontier)
    for binding in element["inputs"]:
        variable = ET.SubElement(inputs, _q("variable"), {"formalParameter": binding["pin"]})
        _connection_point_in(variable, [(input_ids[binding["pin"]], None)])
    # PLCopen TC6 requires the three variable containers in schema order,
    # including an empty inOutVariables container when the block has no IN_OUT pins.
    ET.SubElement(block, _q("inOutVariables"))
    outputs = ET.SubElement(block, _q("outputVariables"))
    for binding in element["outputs"]:
        variable = ET.SubElement(outputs, _q("variable"), {"formalParameter": binding["pin"]})
        _connection_point_out(variable)
    _network_ref(block, network_id)

    for offset, binding in enumerate(element["outputs"]):
        local_id = allocator.next()
        out_variable = ET.SubElement(ld, _q("outVariable"), {"localId": str(local_id)})
        _position(out_variable, x + 200, y + 20 + (offset * 20))
        _connection_point_in(out_variable, [(block_id, binding["pin"])])
        ET.SubElement(out_variable, _q("expression")).text = binding["variable"]
        _network_ref(out_variable, network_id)
    return [(block_id, "Q")], x + 300


def parse_plcopenxml(xml_text: str) -> dict[str, Any]:
    """Parse and structurally verify a renderer-produced PLCopenXML document."""

    if not isinstance(xml_text, str):
        raise LadderRecipeError("PLCopenXML input must be text")
    encoded = xml_text.encode("utf-8")
    if len(encoded) > MAX_XML_BYTES:
        raise LadderRecipeError("PLCopenXML exceeds the bounded one-megabyte limit")
    upper = xml_text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise LadderRecipeError("DTD and entity declarations are forbidden")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise LadderRecipeError("malformed PLCopenXML") from exc
    if root.tag != _q("project"):
        raise LadderRecipeError("unsupported PLCopenXML namespace or root")
    pous = root.findall(f"./{_q('types')}/{_q('pous')}/{_q('pou')}")
    if len(pous) != 1:
        raise LadderRecipeError("PLCopenXML must contain exactly one Ladder POU")
    pou = pous[0]
    recipe_nodes = pou.findall(
        f"./{_q('addData')}/{_q('data')}[@name='universal-plc.ladder-recipe.v1']/{_ax('recipe')}"
    )
    if len(recipe_nodes) != 1 or not recipe_nodes[0].text:
        raise LadderRecipeError("PLCopenXML is missing exact LadderRecipe metadata")
    recipe = _strict_json(recipe_nodes[0].text)
    normalized = validate_ladder_recipe(recipe)
    if recipe_nodes[0].get("sha256") != _semantic_sha(normalized):
        raise LadderRecipeError("LadderRecipe metadata digest mismatch")
    if pou.get("name") != normalized["pou"] or pou.get("pouType") != "program":
        raise LadderRecipeError("PLCopenXML POU identity mismatch")
    _verify_declarations(
        pou, normalized["variables"], _function_block_instances(normalized)
    )
    ld = pou.find(f"./{_q('body')}/{_q('LD')}")
    if ld is None:
        raise LadderRecipeError("PLCopenXML body is not Ladder")
    _verify_networks(ld, normalized)
    expected_xml = render_plcopenxml(normalized)
    if ET.canonicalize(xml_data=xml_text) != ET.canonicalize(xml_data=expected_xml):
        raise LadderRecipeError(
            "PLCopenXML contains content outside the bounded LadderRecipe renderer grammar"
        )
    return normalized


def _verify_declarations(
    pou: ET.Element,
    expected: list[dict[str, Any]],
    instances: list[tuple[str, str]],
) -> None:
    groups = {"input": "inputVars", "output": "outputVars", "local": "localVars"}
    actual: list[dict[str, str]] = []
    interface = pou.find(_q("interface"))
    if interface is None:
        raise LadderRecipeError("PLCopenXML interface is missing")
    for direction in ("input", "output", "local"):
        group = interface.find(_q(groups[direction]))
        if group is None:
            continue
        for variable in group.findall(_q("variable")):
            type_node = variable.find(_q("type"))
            children = list(type_node) if type_node is not None else []
            documentation_node = variable.find(_q("documentation"))
            documentation = (
                ""
                if documentation_node is None
                else "".join(documentation_node.itertext()).strip()
            )
            if len(children) != 1:
                raise LadderRecipeError("PLCopenXML variable type is ambiguous")
            actual_type = (
                children[0].get("name", "")
                if children[0].tag == _q("derived")
                else children[0].tag.removeprefix(f"{{{TC6_NAMESPACE}}}")
            )
            actual.append(
                {
                    "name": variable.get("name", ""),
                    "type": actual_type,
                    "direction": direction,
                    "comment": documentation or "",
                }
            )
    actual_by_name = {item["name"]: item for item in actual}
    expected_with_instances = expected + [
        {"name": name, "type": block_type, "direction": "local", "comment": ""}
        for name, block_type in instances
    ]
    expected_by_name = {item["name"]: item for item in expected_with_instances}
    if actual_by_name != expected_by_name or len(actual) != len(expected_with_instances):
        raise LadderRecipeError("PLCopenXML variable declarations do not match LadderRecipe")


def _network_id(element: ET.Element) -> str | None:
    reference = element.find(
        f"./{_q('addData')}/{_q('data')}[@name='universal-plc.network-ref.v1']/{_ax('networkRef')}"
    )
    return reference.get("id") if reference is not None else None


def _verify_networks(ld: ET.Element, recipe: dict[str, Any]) -> None:
    networks = recipe["networks"]
    in_variables = {
        node.get("localId", ""): node.findtext(_q("expression"), default="")
        for node in ld.findall(_q("inVariable"))
    }
    out_variables = ld.findall(_q("outVariable"))
    for network in networks:
        network_id = network["id"]
        markers = []
        for comment in ld.findall(_q("comment")):
            marker = comment.find(
                f"./{_q('addData')}/{_q('data')}[@name='universal-plc.network.v1']/{_ax('network')}"
            )
            if marker is not None and marker.get("id") == network_id:
                markers.append(marker)
        if len(markers) != 1:
            raise LadderRecipeError(f"network metadata count mismatch: {network_id}")
        marker = markers[0]
        if marker.get("order") != str(network["order"]):
            raise LadderRecipeError(f"network order metadata mismatch: {network_id}")
        if marker.get("semanticSha256") != _semantic_sha(network):
            raise LadderRecipeError(f"network semantic digest mismatch: {network_id}")

        actual_contacts = []
        for contact in ld.findall(_q("contact")):
            if _network_id(contact) == network_id:
                actual_contacts.append(
                    (
                        int(contact.get("localId", "-1")),
                        contact.findtext(_q("variable"), default=""),
                        "NC" if contact.get("negated") == "true" else "NO",
                    )
                )
        actual_contacts.sort()
        expected_contacts = [
            (element["variable"], element["polarity"])
            for element in _walk_elements(network["elements"])
            if element["kind"] == "contact"
        ]
        if [(variable, polarity) for _, variable, polarity in actual_contacts] != expected_contacts:
            raise LadderRecipeError(f"contact structure mismatch: {network_id}")

        actual_blocks = []
        for block in ld.findall(_q("block")):
            if _network_id(block) != network_id:
                continue
            input_bindings = []
            inputs = block.find(_q("inputVariables"))
            if inputs is None:
                raise LadderRecipeError(f"block inputs missing: {network_id}")
            for variable in inputs.findall(_q("variable")):
                pin = variable.get("formalParameter", "")
                if pin == "EN":
                    continue
                connection = variable.find(f"./{_q('connectionPointIn')}/{_q('connection')}")
                if connection is None:
                    raise LadderRecipeError(f"block input connection missing: {network_id}")
                input_bindings.append(
                    {"pin": pin, "variable": in_variables.get(connection.get("refLocalId", ""), "")}
                )
            output_bindings = []
            block_id = block.get("localId", "")
            for out_variable in out_variables:
                if _network_id(out_variable) != network_id:
                    continue
                connection = out_variable.find(f"./{_q('connectionPointIn')}/{_q('connection')}")
                if connection is not None and connection.get("refLocalId") == block_id:
                    output_bindings.append(
                        {
                            "pin": connection.get("formalParameter", ""),
                            "variable": out_variable.findtext(_q("expression"), default=""),
                        }
                    )
            actual_blocks.append(
                {
                    "kind": "fb_call",
                    "block_type": block.get("typeName", ""),
                    "instance": block.get("instanceName", ""),
                    "inputs": input_bindings,
                    "outputs": output_bindings,
                }
            )
        expected_blocks = [
            dict(element)
            for element in _walk_elements(network["elements"])
            if element["kind"] == "fb_call"
        ]
        if actual_blocks != expected_blocks:
            raise LadderRecipeError(f"function-block structure mismatch: {network_id}")

        coils = [
            coil
            for coil in ld.findall(_q("coil"))
            if _network_id(coil) == network_id
        ]
        if len(coils) != 1:
            raise LadderRecipeError(f"network must contain exactly one main coil: {network_id}")
        coil = coils[0]
        if (
            coil.findtext(_q("variable"), default="") != network["coil"]["variable"]
            or coil.get("negated") != "false"
            or coil.get("storage") != "none"
        ):
            raise LadderRecipeError(f"coil structure mismatch: {network_id}")

    actual_refs = {
        network_id
        for element in list(ld)
        if (network_id := _network_id(element)) is not None
    }
    expected_refs = {network["id"] for network in networks}
    if actual_refs != expected_refs:
        raise LadderRecipeError("unexpected or missing network references")
    expected_root = ET.fromstring(render_plcopenxml(recipe))
    expected_ld = expected_root.find(
        f"./{_q('types')}/{_q('pous')}/{_q('pou')}/{_q('body')}/{_q('LD')}"
    )
    if expected_ld is None:
        raise LadderRecipeError("internal LadderRecipe projection failure")
    if _graph_projection(ld) != _graph_projection(expected_ld):
        raise LadderRecipeError("PLCopenXML connection topology does not match LadderRecipe")
def _graph_projection(ld: ET.Element) -> list[tuple[Any, ...]]:
    structural_tags = {_q(tag) for tag in _STRUCTURAL_LD_TAGS}
    allowed_tags = structural_tags | {_q("comment"), _q("vendorElement")}
    unknown_tags = sorted({element.tag for element in list(ld) if element.tag not in allowed_tags})
    if unknown_tags:
        raise LadderRecipeError(
            "unsupported direct LD child element(s): " + ", ".join(unknown_tags)
        )
    nodes = [
        element
        for element in list(ld)
        if element.tag in structural_tags
    ]
    id_to_key: dict[str, tuple[str, str, int]] = {}
    occurrences: dict[tuple[str, str], int] = {}
    keyed_nodes: list[tuple[ET.Element, tuple[str, str, int]]] = []
    for node in nodes:
        local_id = node.get("localId")
        if local_id is None or not local_id.isdecimal() or int(local_id) < 0:
            raise LadderRecipeError("every structural LD node requires a non-negative localId")
        if local_id in id_to_key:
            raise LadderRecipeError(f"duplicate PLCopenXML localId: {local_id}")
        tag = node.tag.removeprefix(f"{{{TC6_NAMESPACE}}}")
        network_id = _network_id(node)
        if network_id is None and tag in {"leftPowerRail", "rightPowerRail"}:
            network_id = "__power_rails__"
        if network_id is None:
            raise LadderRecipeError("every structural LD node requires a network reference")
        occurrence_key = (network_id, tag)
        occurrence = occurrences.get(occurrence_key, 0) + 1
        occurrences[occurrence_key] = occurrence
        key = (network_id, tag, occurrence)
        id_to_key[local_id] = key
        keyed_nodes.append((node, key))

    projection: list[tuple[Any, ...]] = []
    for node, key in keyed_nodes:
        tag = key[1]
        details: tuple[Any, ...]
        if tag == "contact":
            details = (node.get("negated"), node.findtext(_q("variable"), default=""))
        elif tag in {"inVariable", "outVariable"}:
            details = (node.findtext(_q("expression"), default=""),)
        elif tag == "block":
            inputs = node.find(_q("inputVariables"))
            outputs = node.find(_q("outputVariables"))
            details = (
                node.get("typeName"),
                node.get("instanceName"),
                tuple(
                    variable.get("formalParameter", "")
                    for variable in inputs.findall(_q("variable"))
                ) if inputs is not None else (),
                tuple(
                    variable.get("formalParameter", "")
                    for variable in outputs.findall(_q("variable"))
                ) if outputs is not None else (),
            )
        elif tag == "coil":
            details = (
                node.get("negated"),
                node.get("storage"),
                node.findtext(_q("variable"), default=""),
            )
        else:
            details = ()
        connections = []
        for connection in node.iter(_q("connection")):
            source = connection.get("refLocalId", "")
            if source not in id_to_key:
                raise LadderRecipeError(f"connection references unknown localId: {source}")
            source_key = id_to_key[source]
            if source_key[1] == "leftPowerRail":
                source_key = (key[0], source_key[1], source_key[2])
            elif source_key[0] != key[0]:
                raise LadderRecipeError("connection crosses a Ladder network boundary")
            connections.append((source_key, connection.get("formalParameter")))
        projection.append((key, details, tuple(connections)))
    return projection


def semantic_digest(recipe: Mapping[str, Any]) -> str:
    """Return the stable digest used by XML metadata and deployment manifests."""

    return _semantic_sha(validate_ladder_recipe(recipe))
