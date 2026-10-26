# -*- coding: utf-8 -*-
"""Python-2-compatible bounded LadderRecipe validator and PLCopenXML projector.

This module is loaded by the DIADesigner worker from its fixed sibling path.
It intentionally has no filesystem, process, eval, import, or online surface.
"""
from __future__ import print_function

import hashlib
import io
import json
import re
import sys
import xml.etree.ElementTree as ET


TC6_NAMESPACE = "http://www.plcopen.org/xml/tc6_0200"
AX_NAMESPACE = "https://universal-plc.invalid/axmcp/ladder/v1"
XHTML_NAMESPACE = "http://www.w3.org/1999/xhtml"
MAX_XML_BYTES = 1024 * 1024
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NETWORK_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")

FB_SIGNATURES = {
    "R_TRIG": ({"CLK": "BOOL"}, {"Q": "BOOL"}),
    "TON": ({"IN": "BOOL", "PT": "TIME"}, {"Q": "BOOL", "ET": "TIME"}),
    "TP": ({"IN": "BOOL", "PT": "TIME"}, {"Q": "BOOL", "ET": "TIME"}),
}

try:
    TEXT_TYPES = (basestring,)  # noqa: F821 - injected by Python 2.
except NameError:  # pragma: no cover - Python 3 test runtime.
    TEXT_TYPES = (str,)

try:
    ET.register_namespace("", TC6_NAMESPACE)
    ET.register_namespace("ax", AX_NAMESPACE)
except AttributeError:  # pragma: no cover - old ElementTree fallback.
    pass


class LadderImportError(ValueError):
    pass


def _q(name):
    return "{%s}%s" % (TC6_NAMESPACE, name)


def _ax(name):
    return "{%s}%s" % (AX_NAMESPACE, name)


def _xhtml(name):
    return "{%s}%s" % (XHTML_NAMESPACE, name)


def _exact(value, expected, label):
    if not isinstance(value, dict):
        raise LadderImportError(label + " must be an object")
    actual = set(value)
    if actual != set(expected):
        raise LadderImportError(label + " fields are not exact")


def _identifier(value, label):
    if not isinstance(value, TEXT_TYPES) or not IDENTIFIER_RE.match(value):
        raise LadderImportError(label + " must be an IEC identifier")
    return value


def _nonempty(value, label):
    if not isinstance(value, TEXT_TYPES) or not value.strip():
        raise LadderImportError(label + " must be non-empty text")


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strict_json(text):
    def reject_duplicate(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise LadderImportError("duplicate LadderRecipe metadata key")
            value[key] = item
        return value

    def reject_constant(value):
        raise LadderImportError("non-finite LadderRecipe metadata number: " + value)

    try:
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
    except LadderImportError:
        raise
    except Exception as exc:
        raise LadderImportError("invalid LadderRecipe metadata: " + str(exc))


def _semantic_digest(value):
    raw = _canonical_json(value)
    if not isinstance(raw, bytes):
        raw = raw.encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _walk_elements(elements):
    for element in elements:
        if element["kind"] == "branch":
            for path in element["paths"]:
                for child in _walk_elements(path):
                    yield child
        else:
            yield element


def _pin_bindings(bindings, declarations, signature, label, exact):
    if not isinstance(bindings, list) or not bindings:
        raise LadderImportError(label + " must be a non-empty list")
    result = {}
    for index, binding in enumerate(bindings):
        item_label = "%s[%d]" % (label, index)
        _exact(binding, ("pin", "variable"), item_label)
        pin = _identifier(binding["pin"], item_label + ".pin")
        variable = _identifier(binding["variable"], item_label + ".variable")
        if pin in result or pin not in signature:
            raise LadderImportError(item_label + " pin is duplicated or unsupported")
        if variable not in declarations or declarations[variable][0] != signature[pin]:
            raise LadderImportError(item_label + " variable type does not match the pin")
        result[pin] = variable
    if exact and set(result) != set(signature):
        raise LadderImportError(label + " does not bind the exact input signature")
    return result


def _validate_elements(elements, declarations, pou, label, written):
    if not isinstance(elements, list) or not elements:
        raise LadderImportError(label + " must be a non-empty list")
    for index, element in enumerate(elements):
        item_label = "%s[%d]" % (label, index)
        if not isinstance(element, dict):
            raise LadderImportError(item_label + " must be an object")
        kind = element.get("kind")
        if kind == "contact":
            _exact(element, ("kind", "variable", "polarity"), item_label)
            variable = _identifier(element["variable"], item_label + ".variable")
            if variable not in declarations or declarations[variable][0] != "BOOL":
                raise LadderImportError(item_label + " contact must reference a BOOL")
            if element["polarity"] not in ("NO", "NC"):
                raise LadderImportError(item_label + " contact polarity is invalid")
        elif kind == "branch":
            _exact(element, ("kind", "paths"), item_label)
            paths = element["paths"]
            if not isinstance(paths, list) or not 2 <= len(paths) <= 8:
                raise LadderImportError(item_label + " must contain two to eight paths")
            for path_index, path in enumerate(paths):
                if not isinstance(path, list) or not path:
                    raise LadderImportError(item_label + " contains an empty path")
                for path_element in path:
                    if not isinstance(path_element, dict) or path_element.get("kind") != "contact":
                        raise LadderImportError("branch paths may contain contacts only")
                _validate_elements(
                    path,
                    declarations,
                    pou,
                    "%s.paths[%d]" % (item_label, path_index),
                    written,
                )
        elif kind == "fb_call":
            _exact(
                element,
                ("kind", "block_type", "instance", "inputs", "outputs"),
                item_label,
            )
            block_type = element["block_type"]
            if block_type not in FB_SIGNATURES:
                raise LadderImportError(item_label + " function block is unsupported")
            _identifier(element["instance"], item_label + ".instance")
            expected_inputs, allowed_outputs = FB_SIGNATURES[block_type]
            _pin_bindings(
                element["inputs"], declarations, expected_inputs, item_label + ".inputs", True
            )
            outputs = _pin_bindings(
                element["outputs"], declarations, allowed_outputs, item_label + ".outputs", False
            )
            if "Q" not in outputs:
                raise LadderImportError(item_label + " outputs must bind Q")
            for variable in outputs.values():
                if declarations[variable][1] != "local" or variable in written:
                    raise LadderImportError(item_label + " violates local single-writer output")
                written.add(variable)
        else:
            raise LadderImportError(item_label + " element kind is unsupported")


def validate_recipe(recipe):
    _exact(
        recipe,
        ("schema_version", "record_type", "project", "pou", "output_allowlist", "variables", "networks"),
        "LadderRecipe",
    )
    if recipe["schema_version"] != "2" or recipe["record_type"] != "LadderRecipe":
        raise LadderImportError("unsupported LadderRecipe identity")
    _identifier(recipe["project"], "project")
    pou = _identifier(recipe["pou"], "pou")
    allowed_outputs = recipe["output_allowlist"]
    if not isinstance(allowed_outputs, list) or not allowed_outputs:
        raise LadderImportError("explicit nonempty output_allowlist is required")
    for name in allowed_outputs:
        _identifier(name, "output_allowlist")
    if len(set(allowed_outputs)) != len(allowed_outputs):
        raise LadderImportError("duplicate output binding")

    variables = recipe["variables"]
    if not isinstance(variables, list) or not variables:
        raise LadderImportError("variables must be a non-empty list")
    declarations = {}
    for index, variable in enumerate(variables):
        label = "variables[%d]" % index
        _exact(variable, ("name", "type", "direction", "comment"), label)
        name = _identifier(variable["name"], label + ".name")
        if name in declarations:
            raise LadderImportError("duplicate variable declaration")
        if variable["type"] not in ("BOOL", "TIME"):
            raise LadderImportError("unsupported Ladder variable type")
        if variable["direction"] not in ("input", "output", "local"):
            raise LadderImportError("unsupported Ladder variable direction")
        _nonempty(variable["comment"], label + ".comment")
        declarations[name] = (variable["type"], variable["direction"])

    networks = recipe["networks"]
    if not isinstance(networks, list) or not 1 <= len(networks) <= 20:
        raise LadderImportError("networks must contain one to twenty entries")
    ids = set()
    written = set()
    instances = set()
    for index, network in enumerate(networks):
        label = "networks[%d]" % index
        _exact(network, ("id", "order", "title", "comment", "elements", "coil"), label)
        network_id = network["id"]
        if not isinstance(network_id, TEXT_TYPES) or not NETWORK_ID_RE.match(network_id):
            raise LadderImportError(label + " id is invalid")
        if network_id in ids:
            raise LadderImportError("duplicate network ID")
        ids.add(network_id)
        if isinstance(network["order"], bool) or network["order"] != index + 1:
            raise LadderImportError("network order must be contiguous and list-ordered")
        _nonempty(network["title"], label + ".title")
        _nonempty(network["comment"], label + ".comment")
        _validate_elements(network["elements"], declarations, pou, label + ".elements", written)
        for element in _walk_elements(network["elements"]):
            if element["kind"] == "fb_call":
                if element["instance"] in instances:
                    raise LadderImportError("duplicate function-block instance")
                instances.add(element["instance"])
        coil = network["coil"]
        _exact(coil, ("variable", "mode"), label + ".coil")
        coil_name = _identifier(coil["variable"], label + ".coil.variable")
        if coil["mode"] != "normal":
            raise LadderImportError("only normal coils are allowed")
        if declarations.get(coil_name) != ("BOOL", "output"):
            raise LadderImportError("main coil must reference a declared BOOL output")
        if coil_name not in allowed_outputs or coil_name in written:
            raise LadderImportError("main coil is outside the allowlist or has multiple writers")
        written.add(coil_name)
    return json.loads(_canonical_json(recipe))


def semantic_digest(recipe):
    return _semantic_digest(validate_recipe(recipe))


def _position(parent, x, y):
    ET.SubElement(parent, _q("position"), {"x": str(x), "y": str(y)})


def _connection_in(parent, connections):
    point = ET.SubElement(parent, _q("connectionPointIn"))
    ET.SubElement(point, _q("relPosition"), {"x": "0", "y": "0"})
    for local_id, pin in connections:
        attributes = {"refLocalId": str(local_id)}
        if pin:
            attributes["formalParameter"] = pin
        ET.SubElement(point, _q("connection"), attributes)


def _connection_out(parent, formal_parameter=None):
    attributes = {} if formal_parameter is None else {"formalParameter": formal_parameter}
    point = ET.SubElement(parent, _q("connectionPointOut"), attributes)
    ET.SubElement(point, _q("relPosition"), {"x": "0", "y": "0"})


def _network_ref(parent, network_id):
    add_data = ET.SubElement(parent, _q("addData"))
    data = ET.SubElement(
        add_data,
        _q("data"),
        {"name": "universal-plc.network-ref.v1", "handleUnknown": "implementation"},
    )
    ET.SubElement(data, _ax("networkRef"), {"id": network_id})


def _next(allocator):
    allocator[0] += 1
    return allocator[0]


def _indent(element, level=0):
    """Backport ElementTree.indent so ScriptEngine and host bytes stay stable."""
    whitespace = "\n" + ("  " * level)
    children = list(element)
    if children:
        if not element.text or not element.text.strip():
            element.text = whitespace + "  "
        for child in children:
            _indent(child, level + 1)
        if not children[-1].tail or not children[-1].tail.strip():
            children[-1].tail = whitespace
    if level and (not element.tail or not element.tail.strip()):
        element.tail = whitespace


def _render_elements(ld, elements, frontier, x, y, network_id, allocator):
    for element in elements:
        if element["kind"] == "contact":
            local_id = _next(allocator)
            node = ET.SubElement(
                ld,
                _q("contact"),
                {
                    "localId": str(local_id),
                    "negated": "true" if element["polarity"] == "NC" else "false",
                },
            )
            _position(node, x, y)
            _connection_in(node, frontier)
            _connection_out(node)
            ET.SubElement(node, _q("variable")).text = element["variable"]
            _network_ref(node, network_id)
            frontier = [(local_id, None)]
            x += 100
        elif element["kind"] == "branch":
            ends = []
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
            input_ids = {}
            for offset, binding in enumerate(element["inputs"]):
                local_id = _next(allocator)
                input_ids[binding["pin"]] = local_id
                in_variable = ET.SubElement(ld, _q("inVariable"), {"localId": str(local_id)})
                _position(in_variable, x, y + 20 + (offset * 20))
                _connection_out(in_variable)
                ET.SubElement(in_variable, _q("expression")).text = binding["variable"]
                _network_ref(in_variable, network_id)
            block_id = _next(allocator)
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
            _connection_in(enable, frontier)
            for binding in element["inputs"]:
                variable = ET.SubElement(
                    inputs, _q("variable"), {"formalParameter": binding["pin"]}
                )
                _connection_in(variable, [(input_ids[binding["pin"]], None)])
            # PLCopen TC6 mandates input, in-out, output container order.
            # DIADesigner-AX rejects outputVariables when the empty middle
            # container is omitted.
            ET.SubElement(block, _q("inOutVariables"))
            outputs = ET.SubElement(block, _q("outputVariables"))
            for binding in element["outputs"]:
                variable = ET.SubElement(
                    outputs, _q("variable"), {"formalParameter": binding["pin"]}
                )
                _connection_out(variable)
            _network_ref(block, network_id)
            for offset, binding in enumerate(element["outputs"]):
                local_id = _next(allocator)
                out_variable = ET.SubElement(ld, _q("outVariable"), {"localId": str(local_id)})
                _position(out_variable, x + 200, y + 20 + (offset * 20))
                _connection_in(out_variable, [(block_id, binding["pin"])])
                ET.SubElement(out_variable, _q("expression")).text = binding["variable"]
                _network_ref(out_variable, network_id)
            frontier = [(block_id, "Q")]
            x += 300
    return frontier, x


def _render_network(ld, network, allocator):
    network_id = network["id"]
    y = network["order"] * 100
    comment = ET.SubElement(
        ld,
        _q("comment"),
        {"localId": str(_next(allocator)), "height": "40", "width": "600"},
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
            "semanticSha256": _semantic_digest(network),
        },
    )
    vendor = ET.SubElement(ld, _q("vendorElement"), {"localId": str(_next(allocator))})
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
    frontier, x = _render_elements(
        ld, network["elements"], [(0, None)], 100, y, network_id, allocator
    )
    coil_id = _next(allocator)
    coil = ET.SubElement(
        ld,
        _q("coil"),
        {"localId": str(coil_id), "negated": "false", "storage": "none"},
    )
    _position(coil, x, y)
    _connection_in(coil, frontier)
    _connection_out(coil)
    ET.SubElement(coil, _q("variable")).text = network["coil"]["variable"]
    _network_ref(coil, network_id)


def _function_block_instances(recipe):
    return [
        (element["instance"], element["block_type"])
        for network in recipe["networks"]
        for element in _walk_elements(network["elements"])
        if element["kind"] == "fb_call"
    ]


def _plaintext_declaration(recipe, instances):
    lines = ["PROGRAM " + recipe["pou"]]
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
        lines.extend("    %s : %s;" % (item["name"], item["type"]) for item in selected)
        lines.append("END_VAR")
    return "\n".join(lines) + "\n"


def render_plcopenxml(recipe):
    normalized = validate_recipe(recipe)
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
    block_instances = _function_block_instances(normalized)
    groups = {"input": "inputVars", "output": "outputVars", "local": "localVars"}
    for direction in ("input", "output", "local"):
        selected = [item for item in normalized["variables"] if item["direction"] == direction]
        instance_variables = block_instances if direction == "local" else []
        if selected or instance_variables:
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
    ld = ET.SubElement(ET.SubElement(pou, _q("body")), _q("LD"))
    allocator = [0]
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
        normalized, block_instances
    )
    data = ET.SubElement(
        add_data,
        _q("data"),
        {"name": "universal-plc.ladder-recipe.v1", "handleUnknown": "implementation"},
    )
    recipe_node = ET.SubElement(data, _ax("recipe"), {"sha256": _semantic_digest(normalized)})
    recipe_node.text = _canonical_json(normalized)
    instances = ET.SubElement(root, _q("instances"))
    ET.SubElement(instances, _q("configurations"))
    _indent(root)
    buffer = io.BytesIO()
    ET.ElementTree(root).write(buffer, encoding="utf-8", xml_declaration=True)
    raw = buffer.getvalue() + b"\n"
    if len(raw) > MAX_XML_BYTES:
        raise LadderImportError("rendered PLCopenXML exceeds the byte limit")
    return raw


def _pou(root, name):
    matches = [node for node in root.iter(_q("pou")) if node.get("name") == name]
    if len(matches) != 1 or matches[0].get("pouType") != "program":
        raise LadderImportError("round-trip export does not contain the exact program POU")
    return matches[0]


def _ld_body(pou):
    allowed_pou_children = set((_q("interface"), _q("body"), _q("addData")))
    if any(child.tag not in allowed_pou_children for child in list(pou)):
        raise LadderImportError("round-trip POU contains an unsupported direct child")
    bodies = pou.findall(_q("body"))
    if len(bodies) != 1 or bodies[0].attrib:
        raise LadderImportError("round-trip POU must contain exactly one body")
    body_children = list(bodies[0])
    if len(body_children) != 1 or body_children[0].tag != _q("LD"):
        raise LadderImportError("round-trip body must contain exactly one LD implementation")
    return body_children[0]


def _network_label(network):
    return "AXMCP[%s|%s|%s] %s | %s" % (
        network["id"],
        network["order"],
        _semantic_digest(network),
        network["title"],
        network["comment"],
    )


def _direct_connections(node):
    result = []
    point = node.find(_q("connectionPointIn"))
    if point is not None:
        for connection in point.findall(_q("connection")):
            result.append(
                (
                    connection.get("formalParameter"),
                    connection.get("refLocalId", ""),
                )
            )
    if node.tag == _q("block"):
        inputs = node.find(_q("inputVariables"))
        if inputs is not None:
            for variable in inputs.findall(_q("variable")):
                formal = variable.get("formalParameter")
                point = variable.find(_q("connectionPointIn"))
                if point is None:
                    continue
                for connection in point.findall(_q("connection")):
                    result.append((formal, connection.get("refLocalId", "")))
    return result


def _validate_delta_vendor_elements(pou, expected_network_count):
    ld = _ld_body(pou)
    children = list(ld)
    seen_local_ids = set()
    for child in children:
        local_id = child.get("localId")
        if local_id is None:
            continue
        if not local_id.isdigit() or local_id in seen_local_ids:
            raise LadderImportError(
                "round-trip LD localId is invalid or duplicated across direct children"
            )
        seen_local_ids.add(local_id)
    vendor_indexes = [
        index for index, node in enumerate(children) if node.tag == _q("vendorElement")
    ]
    if not vendor_indexes:
        return
    if len(vendor_indexes) != expected_network_count:
        raise LadderImportError(
            "round-trip LD vendor network-title count does not match the recipe"
        )
    for index in vendor_indexes:
        node = children[index]
        if (
            index < 1
            or children[index - 1].tag != _q("comment")
            or not _comment_text(children[index - 1]).startswith("AXMCP[")
        ):
            raise LadderImportError(
                "round-trip LD vendor network-title placement is invalid"
            )
        local_id = node.get("localId")
        if set(node.attrib) != set(("localId",)) or local_id is None or not local_id.isdigit():
            raise LadderImportError(
                "round-trip LD vendor network-title identity is invalid"
            )
        direct = list(node)
        if [item.tag for item in direct] != [
            _q("position"),
            _q("alternativeText"),
            _q("addData"),
        ]:
            raise LadderImportError(
                "round-trip LD vendor network-title shape is invalid"
            )
        position, alternative, add_data = direct
        if set(position.attrib.items()) != set((("x", "0"), ("y", "0"))) or list(position):
            raise LadderImportError(
                "round-trip LD vendor network-title position is invalid"
            )
        alternative_children = list(alternative)
        if (
            alternative.attrib
            or len(alternative_children) != 1
            or alternative_children[0].tag != _xhtml("xhtml")
            or alternative_children[0].attrib
            or list(alternative_children[0])
            or "".join(alternative.itertext()).strip()
        ):
            raise LadderImportError(
                "round-trip LD vendor network-title alternative text is invalid"
            )
        data_children = list(add_data)
        if add_data.attrib or len(data_children) != 1:
            raise LadderImportError(
                "round-trip LD vendor network-title addData is invalid"
            )
        data = data_children[0]
        if (
            data.tag != _q("data")
            or data.attrib
            != {
                "name": "http://www.3s-software.com/plcopenxml/fbdelementtype",
                "handleUnknown": "implementation",
            }
        ):
            raise LadderImportError(
                "round-trip LD vendor network-title data identity is invalid"
            )
        element_types = list(data)
        if (
            len(element_types) != 1
            or element_types[0].tag not in ("ElementType", _q("ElementType"))
            or element_types[0].attrib
            or list(element_types[0])
            or (element_types[0].text or "").strip() != "networktitle"
        ):
            raise LadderImportError(
                "round-trip LD contains an unsupported vendor extension"
            )


def _validate_connection_point(
    node, direction, require_source, allow_multiple_sources=False
):
    if node.attrib:
        raise LadderImportError("round-trip LD connection-point attributes are invalid")
    children = list(node)
    if children and children[0].tag == _q("relPosition"):
        position = children.pop(0)
        if set(position.attrib) != set(("x", "y")) or list(position):
            raise LadderImportError("round-trip LD relative position is invalid")
    if direction == "out":
        if children:
            raise LadderImportError("round-trip LD output connection point is invalid")
        return
    if require_source and (
        not children or (not allow_multiple_sources and len(children) != 1)
    ):
        requirement = "one or more sources" if allow_multiple_sources else "exactly one source"
        raise LadderImportError("round-trip LD input must have " + requirement)
    for connection in children:
        if connection.tag != _q("connection") or list(connection):
            raise LadderImportError("round-trip LD connection shape is invalid")
        if set(connection.attrib) - set(("refLocalId", "formalParameter")):
            raise LadderImportError("round-trip LD connection attributes are invalid")
        source = connection.get("refLocalId")
        if source is None or not source.isdigit():
            raise LadderImportError("round-trip LD connection source is invalid")


def _validate_node_add_data(node, allow_call_type):
    add_data = [item for item in list(node) if item.tag == _q("addData")]
    if len(add_data) > 1:
        raise LadderImportError("round-trip LD node addData is duplicated")
    if not add_data:
        return
    container = add_data[0]
    data_items = list(container)
    if container.attrib or len(data_items) != 1 or data_items[0].tag != _q("data"):
        raise LadderImportError("round-trip LD node addData is invalid")
    data = data_items[0]
    if data.get("handleUnknown") != "implementation" or set(data.attrib) != set(
        ("name", "handleUnknown")
    ):
        raise LadderImportError("round-trip LD node vendor data identity is invalid")
    payload = list(data)
    if data.get("name") == "universal-plc.network-ref.v1":
        if (
            len(payload) != 1
            or payload[0].tag != _ax("networkRef")
            or set(payload[0].attrib) != set(("id",))
            or list(payload[0])
        ):
            raise LadderImportError("round-trip LD network reference is invalid")
        return
    if allow_call_type and data.get("name") == (
        "http://www.3s-software.com/plcopenxml/fbdcalltype"
    ):
        if (
            len(payload) != 1
            or payload[0].tag not in ("CallType", _q("CallType"))
            or payload[0].attrib
            or list(payload[0])
            or (payload[0].text or "").strip() != "functionblock"
        ):
            raise LadderImportError("round-trip LD block call type is invalid")
        return
    raise LadderImportError("round-trip LD contains unsupported node vendor data")


def _validate_semantic_node_shape(node):
    tag = node.tag.split("}", 1)[-1]
    children = list(node)
    expected = {
        "contact": (
            "position",
            "connectionPointIn",
            "connectionPointOut",
            "variable",
        ),
        "inVariable": ("position", "connectionPointOut", "expression"),
        "outVariable": ("position", "connectionPointIn", "expression"),
        "block": (
            "position",
            "inputVariables",
            "inOutVariables",
            "outputVariables",
        ),
        "coil": (
            "position",
            "connectionPointIn",
            "connectionPointOut",
            "variable",
        ),
    }.get(tag)
    if expected is None:
        return
    core = [item for item in children if item.tag != _q("addData")]
    if tuple(item.tag.split("}", 1)[-1] for item in core) != expected:
        raise LadderImportError("round-trip LD semantic node shape is invalid")
    position = core[0]
    if set(position.attrib) != set(("x", "y")) or list(position):
        raise LadderImportError("round-trip LD semantic node position is invalid")
    if tag == "contact":
        _validate_connection_point(core[1], "in", True)
        _validate_connection_point(core[2], "out", False)
        leaf = core[3]
        if leaf.attrib or list(leaf) or not (leaf.text or "").strip():
            raise LadderImportError("round-trip LD contact variable is invalid")
    elif tag == "inVariable":
        _validate_connection_point(core[1], "out", False)
        leaf = core[2]
        if leaf.attrib or list(leaf) or not (leaf.text or "").strip():
            raise LadderImportError("round-trip LD input expression is invalid")
    elif tag == "outVariable":
        _validate_connection_point(core[1], "in", True, True)
        leaf = core[2]
        if leaf.attrib or list(leaf) or not (leaf.text or "").strip():
            raise LadderImportError("round-trip LD output expression is invalid")
    elif tag == "coil":
        _validate_connection_point(core[1], "in", True, True)
        _validate_connection_point(core[2], "out", False)
        leaf = core[3]
        if leaf.attrib or list(leaf) or not (leaf.text or "").strip():
            raise LadderImportError("round-trip LD coil variable is invalid")
    else:
        for container, direction in (
            (core[1], "in"),
            (core[2], "inout"),
            (core[3], "out"),
        ):
            if container.attrib:
                raise LadderImportError("round-trip LD block pin container is invalid")
            if direction == "inout" and list(container):
                raise LadderImportError("round-trip LD block in-out pins are unsupported")
            for pin in list(container):
                if pin.tag != _q("variable"):
                    raise LadderImportError("round-trip LD block pin is invalid")
                point_children = list(pin)
                expected_point = _q(
                    "connectionPointOut" if direction == "out" else "connectionPointIn"
                )
                if len(point_children) != 1 or point_children[0].tag != expected_point:
                    raise LadderImportError("round-trip LD block pin shape is invalid")
                _validate_connection_point(
                    point_children[0], "out" if direction == "out" else "in", direction == "in"
                )
    _validate_node_add_data(node, tag == "block")


def _logic_projection(pou):
    ld = _ld_body(pou)
    semantic = set(
        _q(name)
        for name in (
            "leftPowerRail",
            "contact",
            "inVariable",
            "outVariable",
            "block",
            "coil",
        )
    )
    allowed = semantic | set(
        (_q("rightPowerRail"), _q("comment"), _q("vendorElement"))
    )
    if any(node.tag not in allowed for node in list(ld)):
        raise LadderImportError("round-trip LD contains an unsupported direct child")
    nodes = {}
    for node in list(ld):
        if node.tag not in semantic:
            continue
        local_id = node.get("localId")
        if (
            local_id is None
            or not local_id.isdigit()
            or int(local_id) < 0
            or local_id in nodes
        ):
            raise LadderImportError("round-trip LD localId is invalid or duplicated")
        nodes[local_id] = node
        _validate_semantic_node_shape(node)

    memo = {}

    def semantic_attributes(node, allowed, defaults=None):
        defaults = defaults or {}
        unexpected = set(node.attrib) - set(allowed)
        if unexpected:
            raise LadderImportError(
                "round-trip LD contains unsupported semantic attributes"
            )
        return tuple(
            (name, node.get(name, defaults.get(name)))
            for name in sorted(set(allowed) - set(("localId",)))
        )

    def pin_projection(container):
        if container is None:
            return ()
        variables = []
        for item in list(container):
            if item.tag != _q("variable"):
                raise LadderImportError(
                    "round-trip LD block contains an unsupported pin element"
                )
            variables.append(
                semantic_attributes(
                    item,
                    ("formalParameter", "negated", "edge"),
                    {"negated": "false", "edge": "none"},
                )
            )
        return tuple(variables)

    def expression(local_id, stack):
        if local_id in memo:
            return memo[local_id]
        if local_id in stack or local_id not in nodes:
            raise LadderImportError("round-trip LD connection is cyclic or unresolved")
        node = nodes[local_id]
        tag = node.tag.split("}", 1)[-1]
        if tag == "contact":
            details = (
                semantic_attributes(
                    node,
                    ("localId", "negated", "storage", "edge"),
                    {"negated": "false", "storage": "none", "edge": "none"},
                ),
                node.findtext(_q("variable"), ""),
            )
        elif tag in ("inVariable", "outVariable"):
            details = (
                semantic_attributes(node, ("localId",)),
                node.findtext(_q("expression"), ""),
            )
        elif tag == "block":
            inputs = node.find(_q("inputVariables"))
            inouts = node.find(_q("inOutVariables"))
            outputs = node.find(_q("outputVariables"))
            if inouts is not None and list(inouts):
                raise LadderImportError(
                    "round-trip LD block contains unsupported in-out pins"
                )
            details = (
                semantic_attributes(node, ("localId", "typeName", "instanceName")),
                pin_projection(inputs),
                pin_projection(outputs),
            )
        elif tag == "coil":
            details = (
                semantic_attributes(
                    node,
                    ("localId", "negated", "storage", "edge"),
                    {"negated": "false", "storage": "none", "edge": "none"},
                ),
                node.findtext(_q("variable"), ""),
            )
        else:
            details = ()
        parents = []
        for formal, source in _direct_connections(node):
            parents.append((formal or "", expression(source, stack | set((local_id,)))))
        value = (tag, details, tuple(sorted(parents, key=repr)))
        memo[local_id] = value
        return value

    writers = {}
    for local_id, node in nodes.items():
        if node.tag in (_q("coil"), _q("outVariable")):
            variable = (
                node.findtext(_q("variable"), "")
                if node.tag == _q("coil")
                else node.findtext(_q("expression"), "")
            )
            if not variable or variable in writers:
                raise LadderImportError(
                    "round-trip LD writer identity is missing or duplicated"
                )
            connections = _direct_connections(node)
            if not connections:
                raise LadderImportError(
                    "round-trip LD writer must have one or more sources"
                )
            writers[variable] = tuple(
                sorted(
                    (
                        (formal or "", expression(source, set()))
                        for formal, source in connections
                    ),
                    key=repr,
                )
            )
    if not writers:
        raise LadderImportError("round-trip LD has no writer logic")
    semantic_nodes = {
        local_id
        for local_id, node in nodes.items()
        if node.tag not in (_q("coil"), _q("outVariable"))
    }
    if set(memo) != semantic_nodes:
        raise LadderImportError("round-trip LD contains orphan semantic nodes")
    return {
        "writers": sorted(writers.items(), key=lambda item: item[0]),
    }


def _comment_text(comment):
    content = comment.find(_q("content"))
    return "" if content is None else "".join(content.itertext()).strip()


def _network_sections(pou):
    ld = _ld_body(pou)
    structural = set(
        _q(name)
        for name in (
            "leftPowerRail",
            "rightPowerRail",
            "contact",
            "inVariable",
            "outVariable",
            "block",
            "coil",
        )
    )
    sections = []
    current = None
    for node in list(ld):
        if node.tag == _q("comment"):
            text = _comment_text(node)
            if not text.startswith("AXMCP["):
                continue
            if current is not None:
                sections.append(current)
            current = {"label": text, "nodes": []}
            continue
        if node.tag not in structural:
            continue
        if node.tag in (_q("leftPowerRail"), _q("rightPowerRail")):
            continue
        if current is None:
            raise LadderImportError(
                "round-trip LD semantic node precedes its AXMCP network identity"
            )
        current["nodes"].append(node)
    if current is not None:
        sections.append(current)
    if not sections:
        raise LadderImportError("round-trip LD has no AXMCP network sections")

    all_nodes = {}
    owner_by_id = {}
    for node in list(ld):
        if node.tag not in structural:
            continue
        local_id = node.get("localId")
        if local_id is None or not local_id.isdigit() or local_id in all_nodes:
            raise LadderImportError(
                "round-trip network localId is invalid or duplicated"
            )
        all_nodes[local_id] = node
    for section_index, section in enumerate(sections):
        for node in section["nodes"]:
            owner_by_id[node.get("localId")] = section_index

    def assert_owned_graph(local_id, section_index, stack):
        if local_id in stack or local_id not in all_nodes:
            raise LadderImportError(
                "round-trip network connection is cyclic or unresolved"
            )
        node = all_nodes[local_id]
        if node.tag in (_q("leftPowerRail"), _q("rightPowerRail")):
            return
        if owner_by_id.get(local_id) != section_index:
            raise LadderImportError(
                "round-trip Ladder topology crosses an AXMCP network boundary"
            )
        for _formal, source in _direct_connections(node):
            assert_owned_graph(source, section_index, stack | set((local_id,)))

    result = []
    for section_index, section in enumerate(sections):
        nodes = section["nodes"]
        coils = [node for node in nodes if node.tag == _q("coil")]
        if not coils:
            raise LadderImportError(
                "round-trip network must contain at least one coil"
            )
        # DIADesigner normalizes intermediate outVariables into coils.  The
        # recipe's single main output remains the final coil in the network.
        coil_variable = coils[-1].findtext(_q("variable"), "")
        if not coil_variable:
            raise LadderImportError("round-trip network coil identity is missing")
        writers = []
        for node in nodes:
            if node.tag not in (_q("coil"), _q("outVariable")):
                continue
            variable = (
                node.findtext(_q("variable"), "")
                if node.tag == _q("coil")
                else node.findtext(_q("expression"), "")
            )
            if not variable or variable in writers:
                raise LadderImportError(
                    "round-trip network writer identity is missing or duplicated"
                )
            writers.append(variable)
            assert_owned_graph(node.get("localId"), section_index, set())
        result.append((section["label"], coil_variable, tuple(sorted(writers))))
    return result


def _element_projection(node):
    if node is None:
        return None
    return (
        node.tag.split("}", 1)[-1],
        tuple(sorted(node.attrib.items())),
        (node.text or "").strip(),
        tuple(_element_projection(child) for child in list(node)),
    )


def _validate_plaintext_data(data, expected_plaintext):
    if data.attrib != {
        "name": "http://www.3s-software.com/plcopenxml/interfaceasplaintext",
        "handleUnknown": "implementation",
    }:
        raise LadderImportError("round-trip interface plaintext identity is invalid")
    payload = list(data)
    if len(payload) != 1 or payload[0].tag not in (
        "InterfaceAsPlainText",
        _q("InterfaceAsPlainText"),
    ):
        raise LadderImportError("round-trip interface plaintext payload is invalid")
    xhtml = list(payload[0])
    if len(xhtml) != 1 or xhtml[0].tag != _xhtml("xhtml"):
        raise LadderImportError("round-trip interface plaintext text is invalid")
    actual = "".join(xhtml[0].itertext()).replace("\r\n", "\n").strip()
    expected = expected_plaintext.replace("\r\n", "\n").strip()
    if actual != expected:
        raise LadderImportError("round-trip interface plaintext declaration changed")


def _validate_plaintext_add_data(node, expected_plaintext):
    children = list(node)
    if node.attrib or len(children) != 1 or children[0].tag != _q("data"):
        raise LadderImportError("round-trip interface plaintext addData is invalid")
    _validate_plaintext_data(children[0], expected_plaintext)


def _validate_pou_add_data(pou, expected_plaintext):
    containers = pou.findall(_q("addData"))
    if len(containers) != 1 or containers[0].attrib:
        raise LadderImportError("round-trip POU addData is missing or duplicated")
    data_items = list(containers[0])
    if not data_items or any(item.tag != _q("data") for item in data_items):
        raise LadderImportError("round-trip POU addData payload is invalid")
    names = [item.get("name") for item in data_items]
    allowed = set(
        (
            "http://www.3s-software.com/plcopenxml/interfaceasplaintext",
            "universal-plc.ladder-recipe.v1",
        )
    )
    if len(names) != len(set(names)) or set(names) - allowed:
        raise LadderImportError("round-trip POU addData contains unsupported data")
    plaintext_name = "http://www.3s-software.com/plcopenxml/interfaceasplaintext"
    if plaintext_name not in names:
        raise LadderImportError("round-trip POU interface plaintext is missing")
    _validate_plaintext_data(data_items[names.index(plaintext_name)], expected_plaintext)


def _declarations(pou, expected_plaintext):
    interface = pou.find(_q("interface"))
    if interface is None:
        raise LadderImportError("round-trip POU has no interface")
    directions = (("inputVars", "input"), ("outputVars", "output"), ("localVars", "local"))
    allowed_groups = set(_q(name) for name, _direction in directions)
    if any(item.tag not in allowed_groups for item in list(interface)):
        raise LadderImportError(
            "round-trip variable declarations contain an unsupported interface group"
        )
    result = []
    for group_name, direction in directions:
        group = interface.find(_q(group_name))
        if group is None:
            continue
        if group.attrib or any(
            item.tag not in (_q("variable"), _q("addData")) for item in list(group)
        ):
            raise LadderImportError("round-trip variable declaration group is invalid")
        add_data_nodes = group.findall(_q("addData"))
        if len(add_data_nodes) > 1:
            raise LadderImportError("round-trip interface plaintext addData is duplicated")
        if add_data_nodes:
            _validate_plaintext_add_data(add_data_nodes[0], expected_plaintext)
        for variable in group.findall(_q("variable")):
            type_node = variable.find(_q("type"))
            children = list(type_node) if type_node is not None else []
            if len(children) != 1:
                raise LadderImportError("round-trip variable type is ambiguous")
            item_type = children[0].tag.split("}", 1)[-1]
            if item_type == "derived":
                item_type = children[0].get("name", "")
            if any(
                child.tag
                not in (_q("type"), _q("initialValue"), _q("documentation"))
                for child in list(variable)
            ):
                raise LadderImportError(
                    "round-trip variable declaration contains an unsupported element"
                )
            result.append(
                {
                    "name": variable.get("name", ""),
                    "type": item_type,
                    "direction": direction,
                    "attributes": tuple(sorted(variable.attrib.items())),
                    "type_shape": _element_projection(type_node),
                    "initial_value": _element_projection(
                        variable.find(_q("initialValue"))
                    ),
                }
            )
    return sorted(result, key=lambda item: item["name"])


def verify_roundtrip(xml_raw, expected_recipe):
    if isinstance(xml_raw, bytes):
        xml_bytes = xml_raw
        lowered = xml_raw.lower()
        forbidden = (b"<!doctype", b"<!entity")
    elif isinstance(xml_raw, TEXT_TYPES):
        xml_bytes = xml_raw.encode("utf-8")
        lowered = xml_raw.lower()
        forbidden = ("<!doctype", "<!entity")
    else:
        raise LadderImportError("round-trip PLCopenXML text is invalid")
    if not xml_bytes or len(xml_bytes) > MAX_XML_BYTES:
        raise LadderImportError("round-trip PLCopenXML text is oversized")
    if forbidden[0] in lowered or forbidden[1] in lowered:
        raise LadderImportError("DTD and entity declarations are forbidden")
    try:
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        raise LadderImportError("round-trip PLCopenXML is malformed: " + str(exc))
    normalized = validate_recipe(expected_recipe)
    actual_pou = _pou(root, normalized["pou"])
    _validate_delta_vendor_elements(actual_pou, len(normalized["networks"]))
    expected_plaintext = _plaintext_declaration(
        normalized, _function_block_instances(normalized)
    )
    _validate_pou_add_data(actual_pou, expected_plaintext)
    recipe_nodes = actual_pou.findall(
        "./%s/%s[@name='universal-plc.ladder-recipe.v1']/%s"
        % (_q("addData"), _q("data"), _ax("recipe"))
    )
    if recipe_nodes:
        if len(recipe_nodes) != 1 or recipe_nodes[0].get("sha256") != _semantic_digest(normalized):
            raise LadderImportError("round-trip LadderRecipe metadata digest mismatch")
        try:
            embedded = _strict_json(recipe_nodes[0].text or "")
        except Exception as exc:
            raise LadderImportError("round-trip LadderRecipe metadata is invalid: " + str(exc))
        if _canonical_json(validate_recipe(embedded)) != _canonical_json(normalized):
            raise LadderImportError("round-trip embedded LadderRecipe changed")
    expected_root = ET.fromstring(render_plcopenxml(normalized))
    expected_pou = _pou(expected_root, normalized["pou"])
    if _declarations(actual_pou, expected_plaintext) != _declarations(
        expected_pou, expected_plaintext
    ):
        raise LadderImportError("round-trip variable declarations changed")
    if _logic_projection(actual_pou) != _logic_projection(expected_pou):
        raise LadderImportError("round-trip Ladder topology changed")
    expected_sections = _network_sections(expected_pou)
    if _network_sections(actual_pou) != expected_sections:
        raise LadderImportError("round-trip network identity or comments changed")
    return {
        "pou": normalized["pou"],
        "network_count": len(normalized["networks"]),
        "recipe_digest": _semantic_digest(normalized),
    }


def validate_revision_subset(recipe):
    """Qualified revision subset; the legacy larger import API is unchanged."""
    recipe = validate_recipe(recipe)
    if recipe["pou"] != "MachineSupervisor" or len(recipe["networks"]) != 1:
        raise LadderImportError("LD_REVISION_UNSUPPORTED: one MachineSupervisor network required")
    network = recipe["networks"][0]
    elements = network["elements"]
    if (len(elements) != 1 or elements[0]["kind"] != "contact"
            or network["coil"]["mode"] != "normal"):
        raise LadderImportError("LD_REVISION_UNSUPPORTED: one contact and normal coil required")
    expected = {elements[0]["variable"]: "input", network["coil"]["variable"]: "output"}
    if (len(expected) != 2 or len(recipe["variables"]) != 2
            or any(v["type"] != "BOOL" or expected.get(v["name"]) != v["direction"]
                   for v in recipe["variables"])):
        raise LadderImportError("LD_REVISION_UNSUPPORTED: exact two BOOL interface required")
    return recipe


def verify_revision_baseline(xml_raw, recipe):
    """Only polarity may differ from the requested, already supported graph."""
    recipe = validate_revision_subset(recipe)
    errors = []
    for polarity in ("NO", "NC"):
        candidate = json.loads(_canonical_json(recipe))
        candidate["networks"][0]["elements"][0]["polarity"] = polarity
        try:
            return verify_roundtrip(xml_raw, candidate)
        except LadderImportError as exc:
            errors.append(polarity + ": " + str(exc))
    raise LadderImportError("LD_REVISION_UNSUPPORTED: existing graph/interface differs; " + "; ".join(errors))


def _native_tree(raw):
    if isinstance(raw, bytes):
        text = raw.decode("utf-8")
    elif isinstance(raw, TEXT_TYPES):
        text = raw
    else:
        raise LadderImportError("native revision XML must be text or bytes")
    text = text[1:] if text and ord(text[0]) == 0xFEFF else text
    if (not text or len(text.encode("utf-8")) > MAX_XML_BYTES
            or "<!doctype" in text.lower() or "<!entity" in text.lower()):
        raise LadderImportError("native revision XML is invalid or oversized")
    if sys.platform == "cli":
        # AX's old SimpleXMLTreeBuilder rejects valid non-ASCII UTF-8.
        # Parse with the vendor runtime's XML reader, retaining characters.
        from System.Xml import XmlDocument, XmlNodeType
        doc = XmlDocument()
        doc.PreserveWhitespace = True
        doc.XmlResolver = None
        doc.LoadXml(text)
        def expanded(node):
            return ("{%s}%s" % (node.NamespaceURI, node.LocalName)
                    if node.NamespaceURI else node.LocalName)
        def convert(node):
            if node.NamespaceURI:
                raise LadderImportError("unsupported native XML element namespace")
            result = ET.Element(expanded(node))
            for attribute in node.Attributes:
                if attribute.NamespaceURI == "http://www.w3.org/2000/xmlns/":
                    raise LadderImportError("unsupported native XML namespace declaration")
                if attribute.NamespaceURI not in ("", "http://www.w3.org/XML/1998/namespace"):
                    raise LadderImportError("unsupported native XML attribute namespace")
                result.set(expanded(attribute), attribute.Value)
            previous = None
            for child in node.ChildNodes:
                if child.NodeType == XmlNodeType.Element:
                    previous = convert(child)
                    result.append(previous)
                elif child.NodeType in (XmlNodeType.Text, XmlNodeType.CDATA,
                                       XmlNodeType.Whitespace, XmlNodeType.SignificantWhitespace):
                    if previous is None:
                        result.text = (result.text or "") + child.Value
                    else:
                        previous.tail = (previous.tail or "") + child.Value
                else:
                    raise LadderImportError("unsupported native XML node")
            return result
        return convert(doc.DocumentElement)
    return ET.fromstring(text)


def _one(node, path):
    rows = node.findall(path)
    if len(rows) != 1:
        raise LadderImportError("native revision requires exactly one " + path)
    return rows[0]


def _native_projection(node):
    # Formatting is not metadata; text leaves (including interface) remain exact.
    text = node.text or ""
    if len(node) and not text.strip():
        text = ""
    tail = node.tail or ""
    if not tail.strip():
        tail = ""
    return (node.tag, sorted(node.attrib.items()), text, tail,
            [_native_projection(child) for child in node])


def native_metadata(raw):
    """Full target metadata except implementation and vendor timestamps."""
    root = _native_tree(raw)
    obj = _one(root, ".//Single[@Name='Object']")
    obj.remove(_one(obj, "./Single[@Name='Implementation']"))
    for parent in root.iter():
        for child in list(parent):
            if child.tag == "Single" and child.get("Name") == "Timestamp":
                parent.remove(child)
    return _native_projection(root)


def native_identity(raw):
    root = _native_tree(raw)
    meta = _one(root, ".//Single[@Name='MetaObject']")
    return dict((name, _one(meta, "./Single[@Name='%s']" % name).text)
                for name in ("Name", "Guid", "ParentGuid"))


def prepare_native_revision(target_raw, donor_raw, target_name, donor_name, donor_guid):
    """Use AX-generated implementation; retain all original target metadata."""
    target, donor = _native_tree(target_raw), _native_tree(donor_raw)
    target_obj = _one(target, ".//Single[@Name='Object']")
    donor_obj = _one(donor, ".//Single[@Name='Object']")
    if target_obj.get("Type") != donor_obj.get("Type"):
        raise LadderImportError("native revision POU types differ")
    interface_path = "./Single[@Name='Interface']//Single[@Name='TextBlobForSerialisation']"
    old_text, new_text = _one(target_obj, interface_path).text, _one(donor_obj, interface_path).text
    if (not new_text or new_text.splitlines()[0] != "PROGRAM " + donor_name
            or new_text.replace("PROGRAM " + donor_name, "PROGRAM " + target_name, 1) != old_text):
        raise LadderImportError("native revision interface mismatch")
    # This qualified graph has identical ID allocation. Do not mutate allocator
    # metadata to accommodate a larger, not-yet-qualified graph.
    if (_native_projection(_one(target_obj, "./Single[@Name='UniqueIdGenerator']"))
            != _native_projection(_one(donor_obj, "./Single[@Name='UniqueIdGenerator']"))):
        raise LadderImportError("native revision ID allocator differs")
    replacement = _one(donor_obj, "./Single[@Name='Implementation']")
    replacement_raw = ET.tostring(replacement, encoding="utf-8").decode("utf-8")
    if donor_name in replacement_raw or donor_guid.lower() in replacement_raw.lower():
        raise LadderImportError("native revision donor reference leak")
    original = _one(target_obj, "./Single[@Name='Implementation']")
    index = list(target_obj).index(original)
    target_obj.remove(original)
    target_obj.insert(index, replacement)
    # CR in a text leaf is data (native interface uses CRLF), not indentation.
    # XML literal CR would normalize to LF when reopened.
    result = ET.tostring(target, encoding="utf-8").replace(b"\r", b"&#13;")
    if native_metadata(result.decode("utf-8")) != native_metadata(target_raw):
        raise LadderImportError("native revision modified protected metadata")
    return result


def validate_native_callbacks(rows, name, guid, parent_path):
    """The documented import must actually replace exactly the intended object."""
    counts = {"filter": 0, "conflict": 0, "progress": 0}
    for row in rows:
        event = row.get("event")
        if event not in counts or row.get("valid") is not True:
            raise LadderImportError("native revision unexpected import callback")
        counts[event] += 1
        if row.get("guid") != guid or row.get("name") != name:
            raise LadderImportError("native revision callback identity mismatch")
        if event == "filter" and row.get("path") != list(parent_path):
            raise LadderImportError("native revision callback parent mismatch")
    if counts["filter"] != 1 or counts["conflict"] != 1 or counts["progress"] < 1:
        raise LadderImportError("native revision missing or repeated replacement")
