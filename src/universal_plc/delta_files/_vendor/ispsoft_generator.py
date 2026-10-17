#!/usr/bin/env python3
"""
Generador ISPSoft *.ISP para PLC Delta DVP.

Creador: Javier Fernandez - Buenos Aires - Argentina

Versión 2 - multiprograma.

Validado experimentalmente en ISPSoft para:
- Proyectos con una o varias POUs de tipo Program.
- Contactos NO / NC / flanco ascendente / flanco descendente.
- Bobinas NORMAL / SET / RST.
- Lógica serie y paralelo mediante TYPE=6 / LNK_C / LNK_L.
- TMR, CNT, MOV, ADD, SUB, MUL, DIV.
- Comparador >=.
- Variables globales simbólicas.
- Reconstrucción del contenedor .isp con RAW DEFLATE + CRC32.
- Variantes compactas (stream 0xAC) y proyectos ISPSoft de mayor cabecera.

Principio:
Se parte SIEMPRE de un proyecto .isp válido creado por ISPSoft y se conserva
su configuración de CPU/hardware. El generador modifica variables, tarea,
árbol de programas y código Ladder.

Uso:
    python ispsoft_generator.py plantilla.isp programa.json salida.isp --api SE.api --special-devices DVP_SE_special_devices.json --knowledge DVP_SE_full_knowledge.json

Compatibilidad:
1) Formato V1:
    {
      "pou": "MAIN",
      "variables": [...],
      "networks": [...]
    }

2) Formato V2 multiprograma:
    {
      "variables": [...],
      "programs": [
        {"name": "MAIN", "comment": "...", "networks": [...]},
        {"name": "MOTORES", "comment": "...", "networks": [...]},
        {"name": "VALVULAS", "comment": "...", "networks": [...]}
      ]
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
import argparse
import copy
import json
import re
import struct
import zlib


VERSION = "2.4"


# =============================================================================
# Contenedor ISPSoft
# =============================================================================

@dataclass
class ISPContainer:
    data: bytes
    stream_offset: int
    compressed_length: int
    payload: bytes

    @property
    def stream_end(self) -> int:
        return self.stream_offset + self.compressed_length

    @classmethod
    def load(cls, path: str | Path) -> "ISPContainer":
        # Local integration: refuse arbitrary-offset probing/unbounded inflate.
        from ..containers import read_container
        container = read_container(path)
        return cls(container.data, container.offset,
                   len(container.data) - container.offset, container.source)

    def rebuild(self, new_payload: bytes) -> bytes:
        from ..containers import decode
        return decode(self.data).rebuild(new_payload)



# =============================================================================
# Catálogo de instrucciones ISPSoft (*.api)
# =============================================================================

@dataclass
class APIInstruction:
    name: str
    api_number: int
    api_type: int
    enabled: bool
    input_count: int
    output_count: int
    code_template: str
    raw: str


class APILibrary:
    """Lee las librerías de instrucciones *.api instaladas con ISPSoft."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"No se encontró la librería API: {self.path}")
        self.text = self.path.read_text(encoding="cp1252", errors="replace")
        self.instructions: dict[str, APIInstruction] = {}
        self._parse()

    @staticmethod
    def _count_var_blocks(section: str) -> int:
        return len(re.findall(r"\{.*?\}", section, re.S))

    def _parse(self) -> None:
        blocks = re.findall(r"<API>\s*(.*?)\s*</API>", self.text, re.S)
        if not blocks:
            raise ValueError(f"{self.path} no contiene bloques <API> válidos.")
        for block in blocks:
            def field(name: str, default: str | None = None):
                m = re.search(rf"^{re.escape(name)}=(.*)$", block, re.M)
                return m.group(1).strip() if m else default
            name=field("API_NAME")
            number=field("API_NUMBER")
            api_type=field("API_TYPE","0")
            enabled=field("API_EN","1")
            if not name or number is None:
                continue
            mi=re.search(r"<VAR_INPUT>(.*?)</VAR_INPUT>",block,re.S)
            mo=re.search(r"<VAR_OUTPUT>(.*?)</VAR_OUTPUT>",block,re.S)
            mc=re.search(r"<CODE>\s*(.*?)\s*</CODE>",block,re.S)
            inp=mi.group(1) if mi else ""
            out=mo.group(1) if mo else ""
            ins=APIInstruction(
                name=name.upper(), api_number=int(number), api_type=int(api_type),
                enabled=(enabled != "0"), input_count=self._count_var_blocks(inp),
                output_count=self._count_var_blocks(out),
                code_template=mc.group(1).strip() if mc else "", raw=block)
            self.instructions[ins.name]=ins

    def get(self, symbol: str) -> APIInstruction:
        key=symbol.upper()
        if key not in self.instructions:
            raise ValueError(f"La instrucción {symbol!r} no existe en {self.path.name}.")
        return self.instructions[key]

    def api_number(self, symbol: str) -> int:
        return self.get(symbol).api_number

    def __len__(self) -> int:
        return len(self.instructions)


def resolve_api_library_path(spec: dict[str, Any], spec_path: str | Path | None = None, cli_api: str | Path | None = None) -> Path | None:
    if cli_api:
        return Path(cli_api)
    configured=spec.get("plc",{}).get("api_library")
    if not configured:
        return None
    p=Path(configured)
    if p.is_absolute() or spec_path is None:
        return p
    return Path(spec_path).resolve().parent / p


def resolve_instruction_api(net: dict[str, Any], api_library: APILibrary | None) -> tuple[int, APIInstruction | None]:
    symbol=net["symbol"]
    if api_library is not None:
        definition=api_library.get(symbol)
        if "api_num" in net and int(net["api_num"]) != definition.api_number:
            raise ValueError(
                f"{symbol}: api_num={net['api_num']} no coincide con {api_library.path.name}, "
                f"que define API_NUMBER={definition.api_number}.")
        inputs=list(net.get("inputs",[]))
        outputs=list(net.get("outputs",[]))
        if len(inputs) != definition.input_count:
            raise ValueError(f"{symbol}: {api_library.path.name} define {definition.input_count} entradas, pero el JSON contiene {len(inputs)}.")
        if len(outputs) != definition.output_count:
            raise ValueError(f"{symbol}: {api_library.path.name} define {definition.output_count} salidas, pero el JSON contiene {len(outputs)}.")
        return definition.api_number, definition
    if "api_num" not in net:
        raise ValueError(f"La instrucción {symbol!r} no tiene api_num y no se cargó una librería *.api.")
    return int(net["api_num"]), None



# =============================================================================
# Special M / D por modelo de CPU
# =============================================================================

@dataclass
class SpecialDevice:
    address: str
    description: str
    access: str
    latched: bool
    default: Any
    data_type: str


class SpecialDeviceLibrary:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"No se encontró catálogo Special M/D: {self.path}")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.model = raw.get("model", "")
        self.family = raw.get("family", "")
        self.aliases = {str(k).upper(): str(v).upper() for k,v in raw.get("aliases",{}).items()}
        self.devices = {}
        for address,item in raw.get("devices",{}).items():
            a=address.upper()
            self.devices[a]=SpecialDevice(
                address=a,
                description=item.get("description",""),
                access=item.get("access",""),
                latched=bool(item.get("latched",False)),
                default=item.get("default"),
                data_type=item.get("data_type","BOOL" if a.startswith("M") else "WORD"),
            )

    def resolve(self, key: str) -> SpecialDevice:
        k=key.upper()
        address=self.aliases.get(k,k)
        if address not in self.devices:
            raise ValueError(f"Special device {key!r} no existe en {self.path.name}.")
        return self.devices[address]

    def __len__(self):
        return len(self.devices)


def resolve_special_library_path(spec, spec_path=None, cli_special=None):
    if cli_special:
        return Path(cli_special)
    configured=spec.get("plc",{}).get("special_devices_library")
    if not configured:
        return None
    p=Path(configured)
    if p.is_absolute() or spec_path is None:
        return p
    return Path(spec_path).resolve().parent / p


def prepare_spec_for_special_devices(spec, special_library):
    prepared=copy.deepcopy(spec)
    for var in prepared.get("variables",[]):
        requested=var.get("special") or var.get("special_device")
        if not requested:
            continue
        if special_library is None:
            raise ValueError(f"{var.get('name')}: usa special={requested!r} pero no se cargó catálogo Special M/D.")
        dev=special_library.resolve(str(requested))
        if "device" in var and str(var["device"]).upper()!=dev.address:
            raise ValueError(f"{var.get('name')}: device={var['device']} no coincide con {requested}->{dev.address}.")
        var["device"]=dev.address
        var.setdefault("data_type",dev.data_type)
        var.setdefault("comment",dev.description)
        var["_special_access"]=dev.access
        var["_special_latched"]=dev.latched
        var["_special_default"]=dev.default
    return prepared


def validate_special_device_usage(spec, special_library):
    warnings=[]
    if special_library is None:
        return warnings
    for var in spec.get("variables",[]):
        device=str(var.get("device","")).upper()
        m=re.fullmatch(r"([MD])(\d+)",device)
        if not m:
            continue
        n=int(m.group(2))
        if 1000 <= n <= 1999 and device not in special_library.devices:
            warnings.append(f"{device} está en rango Special {m.group(1)} pero no está documentado en {special_library.path.name}.")
    return warnings



# =============================================================================
# Base de conocimiento DVP por modelo
# =============================================================================

@dataclass
class KnowledgeInstruction:
    symbol: str
    internal_api_num: int
    internal_api_type: int
    official_delta_api: int | None
    manual_function: str
    manual_category: str
    manual_steps_16: int | None
    manual_steps_32: int | None
    inputs: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    code_template: str


class KnowledgeBase:
    """
    Base cruzada entre:
      - manual de programación Delta,
      - librería interna ISPSoft *.api,
      - perfil de memoria/recursos de la CPU.

    No reemplaza SE.api: lo complementa con semántica y restricciones.
    """

    def __init__(self, path: str | Path):
        self.path=Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"No se encontró knowledge base: {self.path}")
        raw=json.loads(self.path.read_text(encoding="utf-8"))
        self.metadata=raw.get("metadata",{})
        self.cpu_profile=raw.get("cpu_profile",{})
        self.instructions={}
        for item in raw.get("ispsoft_internal_instructions",[]):
            symbol=str(item.get("ispsoft_symbol","")).upper()
            if not symbol:
                continue
            self.instructions[symbol]=KnowledgeInstruction(
                symbol=symbol,
                internal_api_num=int(item.get("internal_api_num",-1)),
                internal_api_type=int(item.get("internal_api_type",0)),
                official_delta_api=(
                    int(item["official_delta_api"])
                    if item.get("official_delta_api") is not None
                    else None
                ),
                manual_function=item.get("manual_function",""),
                manual_category=item.get("manual_category",""),
                manual_steps_16=item.get("manual_steps_16"),
                manual_steps_32=item.get("manual_steps_32"),
                inputs=list(item.get("inputs",[])),
                outputs=list(item.get("outputs",[])),
                code_template=item.get("code_template",""),
            )

    def get(self,symbol:str)->KnowledgeInstruction:
        key=symbol.upper()
        if key not in self.instructions:
            raise ValueError(f"La instrucción {symbol!r} no existe en {self.path.name}.")
        return self.instructions[key]

    def __len__(self):
        return len(self.instructions)


def resolve_knowledge_path(spec, spec_path=None, cli_knowledge=None):
    if cli_knowledge:
        return Path(cli_knowledge)
    configured=spec.get("plc",{}).get("knowledge_base")
    if not configured:
        return None
    p=Path(configured)
    if p.is_absolute() or spec_path is None:
        return p
    return Path(spec_path).resolve().parent / p


def _operand_device_class(value: Any, variables_by_name: dict[str,str]) -> str | None:
    """
    Convierte un operando a la clase usada por SE.api:
    K, H, X, Y, M, S, T, C, D, E, F, P, N, KnX/KnY/KnM/KnS.
    """
    token=str(value).strip()
    if token in variables_by_name:
        token=str(variables_by_name[token]).strip()

    if re.fullmatch(r"[+-]?\d+",token):
        return "K"
    if re.fullmatch(r"H[0-9A-Fa-f]+",token):
        return "H"

    m=re.fullmatch(r"K([1-8])([XYMS])(\d+)",token,re.I)
    if m:
        return "Kn"+m.group(2).upper()

    m=re.fullmatch(r"([XYMSTDCEFPNS])(\d+)",token,re.I)
    if m:
        return m.group(1).upper()

    return None


def _allowed_operand(actual: str | None, allowed: list[str]) -> bool:
    if actual is None or not allowed:
        return True
    normalized={str(x).strip() for x in allowed}
    if actual in normalized:
        return True
    # KnM is sometimes represented generically in the table as M/KnM.
    if actual.startswith("Kn") and actual[2:] in normalized:
        return True
    return False


def validate_cpu_device_address(device: str, knowledge: KnowledgeBase) -> None:
    """
    Validación del mapa principal del DVP-SE. X/Y se interpretan en OCTAL.
    """
    d=str(device).upper()
    m=re.fullmatch(r"([XYMSTDCEFP])(\d+)",d)
    if not m:
        # Expresiones como K4M100 no son variables físicas declaradas.
        return
    prefix,numtxt=m.groups()

    if prefix in {"X","Y"}:
        if any(ch not in "01234567" for ch in numtxt):
            raise ValueError(
                f"Dirección {d} inválida: X/Y del DVP usan octal "
                "(después de 77 sigue 100)."
            )
        value=int(numtxt,8)
        if value > int("377",8):
            raise ValueError(f"Dirección {d} fuera del rango {prefix}0-{prefix}377.")
        return

    limits={
        "M":4095,
        "S":1023,
        "T":255,
        "C":254,
        "D":11999,
        "E":7,
        "F":7,
        "P":255,
    }
    if prefix in limits and int(numtxt)>limits[prefix]:
        raise ValueError(f"Dirección {d} fuera del rango DVP-SE para {prefix}.")


def validate_with_knowledge(
    spec: dict[str,Any],
    knowledge: KnowledgeBase | None,
    api_library: APILibrary | None,
) -> list[str]:
    warnings=[]
    if knowledge is None:
        return warnings

    variables_by_name={
        str(v.get("name")):str(v.get("device",""))
        for v in spec.get("variables",[])
        if v.get("name") and v.get("device")
    }

    # Validar mapa de dispositivos declarados.
    for name,device in variables_by_name.items():
        try:
            validate_cpu_device_address(device,knowledge)
        except ValueError as e:
            raise ValueError(f"{name}: {e}")

    programs=normalize_programs(spec)
    estimated_steps=0

    for program in programs:
        for net in normalize_network_ids(program.get("networks",[])):
            kind=net.get("kind","coil").lower()

            if kind=="instruction":
                symbol=str(net["symbol"]).upper()
                kdef=knowledge.get(symbol)

                if api_library is not None:
                    api_def=api_library.get(symbol)
                    if api_def.api_number != kdef.internal_api_num:
                        raise ValueError(
                            f"{symbol}: inconsistencia entre {api_library.path.name} "
                            f"({api_def.api_number}) y {knowledge.path.name} "
                            f"({kdef.internal_api_num})."
                        )

                ins=list(net.get("inputs",[]))
                outs=list(net.get("outputs",[]))
                if len(ins)!=len(kdef.inputs) or len(outs)!=len(kdef.outputs):
                    raise ValueError(
                        f"{symbol}: knowledge base espera {len(kdef.inputs)} entradas/"
                        f"{len(kdef.outputs)} salidas; JSON tiene "
                        f"{len(ins)}/{len(outs)}."
                    )

                for idx,(value,definition) in enumerate(zip(ins,kdef.inputs),1):
                    actual=_operand_device_class(value,variables_by_name)
                    allowed=list(definition.get("allowed_devices",[]))
                    if not _allowed_operand(actual,allowed):
                        raise ValueError(
                            f"{symbol} entrada {idx} ({definition.get('name','?')}): "
                            f"{value!r} es clase {actual}, permitidos={allowed}."
                        )

                for idx,(value,definition) in enumerate(zip(outs,kdef.outputs),1):
                    actual=_operand_device_class(value,variables_by_name)
                    allowed=list(definition.get("allowed_devices",[]))
                    if not _allowed_operand(actual,allowed):
                        raise ValueError(
                            f"{symbol} salida {idx} ({definition.get('name','?')}): "
                            f"{value!r} es clase {actual}, permitidos={allowed}."
                        )

                if kdef.manual_steps_16 is not None:
                    estimated_steps += int(kdef.manual_steps_16)

            elif kind=="coil":
                # Estimación conservadora: contactos + bobina.
                def count_contacts(expr):
                    typ=expr.get("type")
                    if typ=="contact": return 1
                    if typ=="series": return sum(count_contacts(x) for x in expr.get("items",[]))
                    if typ=="parallel": return sum(count_contacts(x) for x in expr.get("branches",[]))
                    return 0
                estimated_steps += count_contacts(net["logic"]) + 1
            elif kind=="comparator":
                estimated_steps += 4
            elif kind=="fb_call":
                # CALL overhead orientativo; el cuerpo del FB se cuenta en su POU.
                estimated_steps += 3

    for fb in normalize_function_blocks(spec):
        for net in normalize_network_ids(fb.get("networks",[])):
            kind=net.get("kind","coil").lower()
            if kind=="instruction":
                kdef=knowledge.get(str(net["symbol"]).upper())
                if kdef.manual_steps_16 is not None:
                    estimated_steps += int(kdef.manual_steps_16)
            elif kind=="coil":
                def fb_count_contacts(expr):
                    typ=expr.get("type")
                    if typ=="contact": return 1
                    if typ=="series": return sum(fb_count_contacts(x) for x in expr.get("items",[]))
                    if typ=="parallel": return sum(fb_count_contacts(x) for x in expr.get("branches",[]))
                    return 0
                estimated_steps += fb_count_contacts(net["logic"])+1
            elif kind=="comparator":
                estimated_steps += 4

    capacity=knowledge.cpu_profile.get("program_capacity_steps")
    if capacity:
        pct=100.0*estimated_steps/int(capacity)
        warnings.append(
            f"Estimación orientativa de programa: ~{estimated_steps} steps "
            f"({pct:.1f}% de {capacity})."
        )
        if pct>80:
            warnings.append("Uso estimado >80%: conviene revisar optimizaciones de memoria.")

    warnings.extend(analyze_optimization_patterns(spec))
    return warnings


def analyze_optimization_patterns(spec: dict[str,Any]) -> list[str]:
    """
    Detecta implementaciones funcionales pero poco compactas.
    No transforma código automáticamente si el patrón aún no fue validado
    completamente en el backend.
    """
    warnings=[]
    for program in normalize_programs(spec):
        nets=normalize_network_ids(program.get("networks",[]))
        groups={}
        for net in nets:
            if net.get("kind","").lower()!="comparator":
                continue
            symbol=str(net.get("symbol",""))
            inputs=list(net.get("inputs",[]))
            if symbol not in {"=","==","LD="} or len(inputs)!=2:
                continue
            source=str(inputs[0])
            try:
                const=int(str(inputs[1]))
            except ValueError:
                continue
            groups.setdefault(source,[]).append((const,net))
        for source,items in groups.items():
            constants=sorted(c for c,_ in items)
            if len(constants)>=16 and constants==list(range(constants[0],constants[0]+len(constants))):
                warnings.append(
                    f"OPTIMIZACIÓN: {program['name']} contiene {len(constants)} "
                    f"comparaciones one-hot consecutivas sobre {source}. "
                    "En DVP-SE considerar matriz compacta ZRST + MBWR + BMOV "
                    "en lugar de una comparación por paso."
                )
    return warnings


# =============================================================================
# Representación Ladder
# =============================================================================

CONTACT_TYPES = {
    "NO": 1,
    "NC": 2,
    "RISING": 3,
    "FALLING": 4,
}

COIL_TYPES = {
    "NORMAL": 13,
    "SET": 15,
    "RST": 16,
}


def contact(name: str, kind: str = "NO") -> dict[str, Any]:
    return {"type": "contact", "name": name, "kind": kind.upper()}


def series(*items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "series", "items": list(items)}


def parallel(*branches: dict[str, Any]) -> dict[str, Any]:
    return {"type": "parallel", "branches": list(branches)}


def coil(name: str, kind: str = "NORMAL") -> dict[str, Any]:
    return {"type": "coil", "name": name, "kind": kind.upper()}


def node_contact(name: str, kind: str = "NO") -> str:
    kind = kind.upper()
    if kind not in CONTACT_TYPES:
        raise ValueError(f"Tipo de contacto no soportado: {kind}")

    return (
        "[LD_NODE]\n"
        f"TYPE={CONTACT_TYPES[kind]}\n"
        f"DEV_NAME={name}\n"
        "[END_LD_NODE]"
    )


def node_filler() -> str:
    return (
        "[LD_NODE]\n"
        "TYPE=5\n"
        "DEV_NAME=\n"
        "[END_LD_NODE]"
    )


def node_parallel(branch_count: int, branch_length: int) -> str:
    return (
        "[LD_NODE]\n"
        "TYPE=6\n"
        f"LNK_C={branch_count}\n"
        f"LNK_L={branch_length}\n"
        "[END_LD_NODE]"
    )


def node_coil(name: str, kind: str = "NORMAL") -> str:
    kind = kind.upper()
    if kind not in COIL_TYPES:
        raise ValueError(f"Tipo de bobina no soportado: {kind}")

    return (
        "[LD_NODE]\n"
        f"TYPE={COIL_TYPES[kind]}\n"
        f"DEV_NAME={name}\n"
        "[END_LD_NODE]"
    )


def _flatten_branch(expr: dict[str, Any]) -> list[str]:
    """
    Serializa una rama individual de un paralelo.

    Se admiten contactos individuales o series de contactos.
    Los paralelos anidados dentro de una rama todavía se rechazan de forma
    explícita porque su matriz TYPE=5/TYPE=6 no fue validada suficientemente.
    """
    typ = expr.get("type")

    if typ == "contact":
        return [node_contact(expr["name"], expr.get("kind", "NO"))]

    if typ == "series":
        result: list[str] = []
        for item in expr.get("items", []):
            if item.get("type") == "parallel":
                raise ValueError(
                    "Paralelos anidados dentro de una rama no están "
                    "habilitados todavía."
                )
            result.extend(_flatten_branch(item))
        return result

    raise ValueError(
        f"Elemento no válido dentro de una rama paralela: {typ}"
    )


def serialize_logic(expr: dict[str, Any]) -> list[str]:
    typ = expr.get("type")

    if typ == "contact":
        return [node_contact(expr["name"], expr.get("kind", "NO"))]

    if typ == "series":
        result: list[str] = []
        for item in expr.get("items", []):
            result.extend(serialize_logic(item))
        return result

    if typ == "parallel":
        branches = expr.get("branches", [])
        if len(branches) < 2:
            raise ValueError("Un paralelo necesita al menos 2 ramas.")

        serialized = [_flatten_branch(branch) for branch in branches]
        width = max(len(branch) for branch in serialized)

        result: list[str] = []
        for branch in serialized:
            result.extend(branch)
            result.extend(
                node_filler() for _ in range(width - len(branch))
            )

        result.append(node_parallel(len(branches), width))
        return result

    raise ValueError(
        f"Tipo de expresión Ladder no soportado: {typ}"
    )


# =============================================================================
# Networks
# =============================================================================

def network_properties(net_id: int, comment: str = "") -> str:
    return f"""<NETWORK_START>
<PROPERTIES_START>
NET_ID={net_id}
NET_LABEL=
NET_ACTIVE=TRUE
NET_BOOKMARK=FALSE
NET_MODIFY=FALSE
NET_FOLD=FALSE
(*
{comment}
*)
<PROPERTIES_END>"""


def serialize_coil_network(net: dict[str, Any]) -> str:
    root_nodes = serialize_logic(net["logic"])
    output = net["coil"]

    return (
        network_properties(net["id"], net.get("comment", ""))
        + "\n<ROOTLINK_START>\n"
        + "\n".join(root_nodes)
        + "\n<ROOTLINK_END>\n"
        + "<OUTLINK_START>\n"
        + node_coil(
            output["name"],
            output.get("kind", "NORMAL"),
        )
        + "\n<OUTLINK_END>\n"
        + "<NETWORK_END>"
    )


def instruction_nodes(
    symbol: str,
    api_num: int,
    inputs: Iterable[str] = (),
    outputs: Iterable[str] = (),
) -> str:
    inputs = list(inputs)
    outputs = list(outputs)
    chunks: list[str] = []

    if inputs:
        chunks.append(
            "[LD_NODE]\n"
            "TYPE=11\n"
            "<VAR_NODE_S>\n"
            + "".join(f"VAR_NAME={value}\n" for value in inputs)
            + "<VAR_NODE_E>\n"
            "[END_LD_NODE]"
        )

    chunks.append(
        "[LD_NODE]\n"
        "TYPE=9\n"
        f"SYMB={symbol}\n"
        "DEV_NAME=\n"
        f"API_NUM={api_num}\n"
        "[END_LD_NODE]"
    )

    if outputs:
        chunks.append(
            "[LD_NODE]\n"
            "TYPE=12\n"
            "<VAR_NODE_S>\n"
            + "".join(f"VAR_NAME={value}\n" for value in outputs)
            + "<VAR_NODE_E>\n"
            "[END_LD_NODE]"
        )
    else:
        chunks.append(
            "[LD_NODE]\n"
            "TYPE=12\n"
            "[END_LD_NODE]"
        )

    chunks.append(
        "[LD_NODE]\n"
        "TYPE=7\n"
        "[END_LD_NODE]"
    )

    return "\n".join(chunks)


def serialize_instruction_network(net: dict[str, Any]) -> str:
    enable_nodes = serialize_logic(net["enable"])
    api_num = net.get("_resolved_api_num", net.get("api_num"))
    if api_num is None:
        raise ValueError(f"La instrucción {net.get('symbol')!r} no tiene API_NUM resuelto.")
    instruction = instruction_nodes(
        net["symbol"],
        int(api_num),
        net.get("inputs", []),
        net.get("outputs", []),
    )

    return (
        network_properties(net["id"], net.get("comment", ""))
        + "\n<ROOTLINK_START>\n"
        + "\n".join(enable_nodes)
        + "\n<ROOTLINK_END>\n"
        + "<OUTLINK_START>\n"
        + instruction
        + "\n<OUTLINK_END>\n"
        + "<NETWORK_END>"
    )


def serialize_comparator_network(net: dict[str, Any]) -> str:
    enable_nodes = serialize_logic(net["enable"])
    args = net.get("inputs", [])

    comparator = (
        "[LD_NODE]\n"
        "TYPE=11\n"
        "<VAR_NODE_S>\n"
        + "".join(f"VAR_NAME={value}\n" for value in args)
        + "<VAR_NODE_E>\n"
        "[END_LD_NODE]\n"
        "[LD_NODE]\n"
        "TYPE=10\n"
        f"SYMB={net['symbol']}\n"
        "DEV_NAME=\n"
        f"API_NUM={int(net.get('api_num', -1))}\n"
        "[END_LD_NODE]\n"
        "[LD_NODE]\n"
        "TYPE=12\n"
        "[END_LD_NODE]\n"
        "[LD_NODE]\n"
        "TYPE=7\n"
        "[END_LD_NODE]"
    )

    output = net["coil"]

    return (
        network_properties(net["id"], net.get("comment", ""))
        + "\n<ROOTLINK_START>\n"
        + "\n".join(enable_nodes)
        + "\n"
        + comparator
        + "\n<ROOTLINK_END>\n"
        + "<OUTLINK_START>\n"
        + node_coil(
            output["name"],
            output.get("kind", "NORMAL"),
        )
        + "\n<OUTLINK_END>\n"
        + "<NETWORK_END>"
    )


def serialize_network(net: dict[str, Any]) -> str:
    kind = net.get("kind", "coil").lower()

    if kind == "coil":
        return serialize_coil_network(net)

    if kind == "instruction":
        return serialize_instruction_network(net)

    if kind == "comparator":
        return serialize_comparator_network(net)

    raise ValueError(f"Tipo de Network no soportado: {kind}")



# =============================================================================
# Function Blocks e instancias
# =============================================================================

FB_VAR_SCOPES = {"VAR_INPUT", "VAR_OUTPUT", "VAR", "VAR_IN_OUT"}

def serialize_fb_local_variables(variables: list[dict[str, Any]]) -> str:
    chunks=["<LOCAL_VAR>","<VAR>"]
    for var in variables:
        name=var["name"]
        dtype=var.get("data_type","BOOL").upper()
        scope=var.get("scope","VAR").upper()
        if scope not in FB_VAR_SCOPES:
            raise ValueError(
                f"{name}: scope FB inválido {scope!r}; "
                f"permitidos={sorted(FB_VAR_SCOPES)}"
            )
        comment=var.get("comment","")
        chunks.extend([
            "{",
            f"{name} : {dtype} :=  [@@] {{{scope}}}",
            "(*",
            comment,
            "*)",
            "}",
        ])
    chunks.extend(["</VAR>","</LOCAL_VAR>"])
    return "\n".join(chunks)


def serialize_function_block(fb: dict[str, Any]) -> str:
    name=fb["name"]
    comment=fb.get("comment","")
    variables=fb.get("variables",[])
    networks=normalize_network_ids(fb.get("networks",[]))
    if not variables:
        raise ValueError(f"FB {name!r} no tiene variables.")
    if not networks:
        raise ValueError(f"FB {name!r} no tiene Networks.")
    local_text=serialize_fb_local_variables(variables)
    network_text="\n".join(serialize_network(n) for n in networks)
    return f"""<POU>
P_Name={name}
P_En_Eno=TRUE
P_Last_Chg=
P_type=1
P_Rtn_Type=
P_Lang=1
P_Step=0
P_Version={fb.get('version','1.00')}
P_DeltaFB=FALSE
P_Security=
P_Active=TRUE
P_Priority=9999
P_DFBName=
(*
{comment}
*)
{local_text}
{network_text}
</POU>"""


def normalize_function_blocks(spec: dict[str, Any]) -> list[dict[str, Any]]:
    blocks=copy.deepcopy(spec.get("function_blocks",[]))
    names=[b.get("name") for b in blocks]
    if any(not n for n in names):
        raise ValueError("Hay un Function Block sin nombre.")
    dup=sorted({n for n in names if names.count(n)>1})
    if dup:
        raise ValueError("Function Blocks duplicados: "+", ".join(dup))
    return blocks


def fb_signature(fb: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    sig={"VAR_INPUT":[],"VAR_IN_OUT":[],"VAR_OUTPUT":[],"VAR":[]}
    for var in fb.get("variables",[]):
        scope=var.get("scope","VAR").upper()
        if scope not in sig:
            raise ValueError(f"FB {fb['name']}: scope inválido {scope}")
        sig[scope].append(var)
    return sig


def serialize_fb_call_network(
    net: dict[str, Any],
    fb_by_name: dict[str, dict[str, Any]],
) -> str:
    fb_name=net.get("fb") or net.get("symbol")
    instance=net.get("instance")
    if not fb_name or fb_name not in fb_by_name:
        raise ValueError(f"FB call: Function Block desconocido {fb_name!r}.")
    if not instance:
        raise ValueError("FB call sin nombre de instancia.")

    sig=fb_signature(fb_by_name[fb_name])
    inputs=list(net.get("inputs",[]))
    inouts=list(net.get("inouts",[]))
    outputs=list(net.get("outputs",[]))

    if len(inputs)!=len(sig["VAR_INPUT"]):
        raise ValueError(
            f"{instance}:{fb_name} requiere {len(sig['VAR_INPUT'])} VAR_INPUT "
            f"y se pasaron {len(inputs)}."
        )
    if len(inouts)!=len(sig["VAR_IN_OUT"]):
        raise ValueError(
            f"{instance}:{fb_name} requiere {len(sig['VAR_IN_OUT'])} VAR_IN_OUT "
            f"y se pasaron {len(inouts)}."
        )
    if len(outputs)!=len(sig["VAR_OUTPUT"]):
        raise ValueError(
            f"{instance}:{fb_name} requiere {len(sig['VAR_OUTPUT'])} VAR_OUTPUT "
            f"y se pasaron {len(outputs)}."
        )

    # ISPSoft coloca VAR_INPUT + VAR_IN_OUT en TYPE=11, y VAR_OUTPUT en TYPE=12.
    in_values=inputs+inouts
    in_node=(
        "[LD_NODE]\nTYPE=11\n<VAR_NODE_S>\n"
        +"".join(f"VAR_NAME={v}\n" for v in in_values)
        +"<VAR_NODE_E>\n[END_LD_NODE]"
    )
    call_node=(
        "[LD_NODE]\n"
        "TYPE=8\n"
        f"SYMB={fb_name}\n"
        f"DEV_NAME={instance}\n"
        "API_NUM=-1\n"
        "[END_LD_NODE]"
    )
    out_node=(
        "[LD_NODE]\nTYPE=12\n<VAR_NODE_S>\n"
        +"".join(f"VAR_NAME={v}\n" for v in outputs)
        +"<VAR_NODE_E>\n[END_LD_NODE]"
    )
    end_node="[LD_NODE]\nTYPE=7\n[END_LD_NODE]"

    return (
        network_properties(net["id"],net.get("comment",""))
        +"\n<ROOTLINK_START>\n"
        +in_node+"\n"+call_node+"\n"+out_node+"\n"+end_node
        +"\n<ROOTLINK_END>\n"
        +"<OUTLINK_START>\n<OUTLINK_END>\n"
        +"<NETWORK_END>"
    )


def parse_auto_symbol_ranges(project_text: str) -> dict[str, tuple[int,int]]:
    """
    Lee la configuración real de Ubicación Símbolos desde <SYSTEM_VAR>.
    """
    m=re.search(r"<SYSTEM_VAR>(.*?)</SYSTEM_VAR>",project_text,re.S)
    if not m:
        raise ValueError("La plantilla no contiene <SYSTEM_VAR>.")
    s=m.group(1)
    mapping={}
    tags={"M":"M","D":"D","T":"T","C":"C","S":"S"}
    for key,tag in tags.items():
        mm=re.search(rf"<{tag}>(\d+)-(\d+)",s)
        if mm:
            mapping[key]=(int(mm.group(1)),int(mm.group(2)))
    missing=[k for k in tags if k not in mapping]
    if missing:
        raise ValueError(
            "Faltan rangos de asignación automática en SYSTEM_VAR: "
            +", ".join(missing)
        )
    return mapping


class DeviceAllocator:
    def __init__(
        self,
        ranges: dict[str,tuple[int,int]],
        occupied: dict[str,set[int]] | None = None,
    ):
        self.ranges=ranges
        self.occupied={k:set() for k in ranges}
        if occupied:
            for k,v in occupied.items():
                self.occupied.setdefault(k,set()).update(v)

    def reserve(self, prefix: str, start: int, width: int=1) -> None:
        for n in range(start,start+width):
            self.occupied.setdefault(prefix,set()).add(n)

    def allocate(self, prefix: str, width: int=1) -> int:
        lo,hi=self.ranges[prefix]
        used=self.occupied.setdefault(prefix,set())
        for start in range(lo,hi-width+2):
            if all((start+i) not in used for i in range(width)):
                self.reserve(prefix,start,width)
                return start
        raise ValueError(
            f"Sin espacio libre para {width} dispositivo(s) {prefix} "
            f"dentro de {lo}-{hi}."
        )


def _explicit_device_usage(spec: dict[str,Any]) -> dict[str,set[int]]:
    used={"M":set(),"D":set(),"T":set(),"C":set(),"S":set()}
    for var in spec.get("variables",[]):
        dev=str(var.get("device","")).upper()
        m=re.fullmatch(r"([MDTCS])(\d+)",dev)
        if not m:
            continue
        prefix,num=m.group(1),int(m.group(2))
        dtype=str(var.get("data_type","")).upper()
        width=2 if prefix=="D" and dtype in {"DWORD","DINT","REAL","LREAL"} else 1
        for i in range(width):
            used[prefix].add(num+i)
    return used


def _fb_storage_class(dtype: str) -> tuple[str,int]:
    t=dtype.upper()
    if t=="BOOL":
        return "M",1
    if t in {"WORD","INT","UINT"}:
        return "D",1
    if t in {"DWORD","DINT","UDINT","REAL"}:
        return "D",2
    if t=="TIMER":
        return "T",1
    if t=="COUNTER":
        return "C",1
    if t=="STEP":
        return "S",1
    raise ValueError(
        f"Tipo {dtype!r} todavía no tiene regla de almacenamiento FB."
    )


def collect_fb_instances(
    spec: dict[str,Any],
    fb_by_name: dict[str,dict[str,Any]],
) -> list[dict[str,Any]]:
    """
    Las instancias globales se declaran en variables con:
      {"name":"MOT1","fb_type":"MOTOR"}

    Las instancias locales se reservan para una próxima validación con una
    muestra ISPSoft real; no se serializan por inferencia.
    """
    instances=[]
    for var in spec.get("variables",[]):
        fb_type=var.get("fb_type")
        if not fb_type:
            continue
        if fb_type not in fb_by_name:
            raise ValueError(
                f"{var.get('name')}: fb_type desconocido {fb_type!r}."
            )
        scope=var.get("instance_scope","global").lower()
        if scope!="global":
            raise ValueError(
                f"{var.get('name')}: instancias FB locales todavía requieren "
                "una muestra ISPSoft para validar su serialización."
            )
        instances.append({
            "name":var["name"],
            "fb_type":fb_type,
            "scope":"Global",
        })
    return instances


def allocate_fb_instances(
    spec: dict[str,Any],
    project_text: str,
    function_blocks: list[dict[str,Any]],
) -> tuple[list[str],dict[str,dict[str,str]]]:
    """
    Reproduce el patrón validado por ISPSoft:
      - cada instancia reserva 2 M ocultas;
      - luego cada BOOL visible del FB recibe una M;
      - WORD/INT reciben D del rango automático;
      - tipos de 32 bits reciben 2 D consecutivos;
      - T/C/S usan sus respectivos rangos automáticos.

    El allocator considera direcciones explícitas ya usadas y nunca repite
    una dirección.
    """
    fb_by_name={fb["name"]:fb for fb in function_blocks}
    instances=collect_fb_instances(spec,fb_by_name)
    ranges=parse_auto_symbol_ranges(project_text)
    allocator=DeviceAllocator(ranges,_explicit_device_usage(spec))

    lines=[]
    allocation={}

    for inst in instances:
        name=inst["name"]
        fb=fb_by_name[inst["fb_type"]]
        allocation[name]={}

        # Dos M ocultas por instancia, patrón verificado en Test_FB/Test_FB3.
        allocator.allocate("M",2)

        for var in fb.get("variables",[]):
            prefix,width=_fb_storage_class(var.get("data_type","BOOL"))
            start=allocator.allocate(prefix,width)
            address=f"{prefix}{start}"
            allocation[name][var["name"]]=address
            lines.append(
                f"{inst['scope']}->{name}->{var['name']}={address}"
            )

    return lines,allocation


def serialize_fb_instances_section(lines: list[str]) -> str:
    if not lines:
        return "<FBInst>\n</FBInst>"
    return "<FBInst>\n"+"\n".join(lines)+"\n</FBInst>"


def serialize_global_variables_v24(
    variables: list[dict[str,Any]],
) -> str:
    chunks=["<GLOBAL_VAR>","TableName=Global Variables"]
    for var in variables:
        name=var["name"]
        comment=var.get("comment","")
        if var.get("fb_type"):
            chunks.extend([
                "{","VAR",name,f";{var['fb_type']};",
                "(*",comment,"*)","}"
            ])
            continue

        if "device" not in var:
            raise ValueError(
                f"Variable global {name!r} sin device ni fb_type."
            )
        dtype=var.get("data_type","BOOL")
        init=var.get("init",0)
        chunks.extend([
            "{","VAR",name,f"{var['device']};{dtype};{init}",
            "(*",comment,"*)","}"
        ])
    chunks.append("</GLOBAL_VAR>")
    return "\n".join(chunks)


def serialize_group_pou_folder_v24(
    program_names: list[str],
    fb_names: list[str],
) -> str:
    programs="\n".join(
        f"""<FolderContent>
ContentType=0
ContentName={name}
ContentPath=
</FolderContent>"""
        for name in program_names
    )
    fbs="\n".join(
        f"""<FolderContent>
ContentType=1
ContentName={name}
ContentPath=
</FolderContent>"""
        for name in fb_names
    )
    return f"""<GroupPOUFolder>
<ProgramFolder>
{programs}
</ProgramFolder>
<FBFolder>
{fbs}
</FBFolder>
</GroupPOUFolder>"""


# =============================================================================
# Variables
# =============================================================================

def serialize_global_variables(
    variables: list[dict[str, Any]]
) -> str:
    chunks = ["<GLOBAL_VAR>", "TableName=Global Variables"]

    for var in variables:
        chunks.extend(
            [
                "{",
                "VAR",
                var["name"],
                (
                    f"{var['device']};"
                    f"{var.get('data_type', 'BOOL')};"
                    f"{var.get('init', 0)}"
                ),
                "(*",
                var.get("comment", ""),
                "*)",
                "}",
            ]
        )

    chunks.append("</GLOBAL_VAR>")
    return "\n".join(chunks)


# =============================================================================
# POUs, Task y árbol de proyecto
# =============================================================================

def normalize_programs(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Convierte V1 y V2 a una única estructura interna.
    """
    if "programs" in spec:
        programs = copy.deepcopy(spec["programs"])
        if not isinstance(programs, list) or not programs:
            raise ValueError("'programs' debe ser una lista no vacía.")
        return programs

    # Compatibilidad con la V1.
    return [
        {
            "name": spec.get("pou", "MAIN"),
            "comment": spec.get("pou_comment", ""),
            "networks": copy.deepcopy(spec.get("networks", [])),
        }
    ]


def normalize_network_ids(
    networks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for index, network in enumerate(networks, 1):
        item = copy.deepcopy(network)
        item.setdefault("id", index)
        result.append(item)

    return result



def serialize_program_network(
    net: dict[str,Any],
    fb_by_name: dict[str,dict[str,Any]] | None = None,
) -> str:
    if net.get("kind","").lower()=="fb_call":
        if fb_by_name is None:
            raise ValueError("FB call sin catálogo de Function Blocks.")
        return serialize_fb_call_network(net,fb_by_name)
    return serialize_network(net)


def serialize_pou(program: dict[str, Any], fb_by_name: dict[str,dict[str,Any]] | None = None) -> str:
    name = program["name"]
    comment = program.get("comment", "")
    networks = normalize_network_ids(
        program.get("networks", [])
    )

    if not networks:
        raise ValueError(
            f"El programa {name!r} no tiene Networks. "
            "Agregá al menos una Network."
        )

    network_text = "\n".join(
        serialize_program_network(network, fb_by_name)
        for network in networks
    )

    return f"""<POU>
P_Name={name}
P_En_Eno={str(program.get('enable_eno', True)).upper()}
P_Last_Chg=
P_type=0
P_Rtn_Type=
P_Lang=1
P_Step=0
P_Version={program.get('version', '1.00')}
P_DeltaFB=FALSE
P_Security=
P_Active={str(program.get('active', True)).upper()}
P_Priority={program.get('priority', 9999)}
P_DFBName=
(*
{comment}
*)
<LOCAL_VAR>
</LOCAL_VAR>
{network_text}
</POU>"""


def serialize_task(
    program_names: list[str],
    task_options: dict[str, Any] | None = None,
) -> str:
    options = task_options or {}

    pnames = "\n".join(
        f"T_PName={name}" for name in program_names
    )

    return f"""<TASK>
[TASK_S]
T_ID={options.get('id', 0)}
T_ACT={str(options.get('active', True)).upper()}
T_SynRedundancyAfterExe=FALSE
T_CONFIG_TYPE={options.get('config_type', 3)}
T_CycTime={options.get('cycle_time', 20)}
T_EventType={options.get('event_type', 1)}
T_EnWatchDog={str(options.get('watchdog_enabled', False)).upper()}
T_WatchDogTime={options.get('watchdog_time', 20)}
{pnames}
[TASK_E]
</TASK>"""


def serialize_group_pou_folder(
    program_names: list[str]
) -> str:
    contents = "\n".join(
        f"""<FolderContent>
ContentType=0
ContentName={name}
ContentPath=
</FolderContent>"""
        for name in program_names
    )

    return f"""<GroupPOUFolder>
<ProgramFolder>
{contents}
</ProgramFolder>
<FBFolder>
</FBFolder>
</GroupPOUFolder>"""


def replace_program_pous(
    text: str,
    programs: list[dict[str, Any]],
) -> str:
    """
    Reemplaza todas las POUs de tipo Program existentes por las generadas.

    Las plantillas que estamos usando contienen sólo Program POUs. Para no
    destruir Function Blocks accidentalmente en plantillas futuras, esta
    función conserva POUs con P_type distinto de 0.
    """
    matches = list(re.finditer(r"<POU>.*?</POU>", text, re.S))
    if not matches:
        raise ValueError("La plantilla no contiene ninguna POU.")

    program_matches = []
    preserved_pous = []

    for match in matches:
        block = match.group(0)
        ptype_match = re.search(r"^P_type=(.+)$", block, re.M)
        ptype = ptype_match.group(1).strip() if ptype_match else "0"

        if ptype == "0":
            program_matches.append(match)
        else:
            preserved_pous.append(block)

    if not program_matches:
        raise ValueError(
            "La plantilla no contiene POUs de tipo Program."
        )

    generated = "\n".join(
        serialize_pou(program)
        for program in programs
    )

    # Sustituimos el rango desde la primera hasta la última POU Program.
    # En las plantillas validadas, las POUs Program son contiguas.
    start = program_matches[0].start()
    end = program_matches[-1].end()

    between = text[start:end]

    # Seguridad: si hay una POU no-Program dentro del rango, no la tocamos
    # automáticamente.
    if any(
        block in between
        for block in preserved_pous
    ):
        raise ValueError(
            "La plantilla mezcla Program y Function Block POUs en el "
            "mismo rango. Usá una plantilla vacía sin FBs para generar "
            "un proyecto multiprograma."
        )

    return text[:start] + generated + text[end:]



def prepare_spec_for_api_library(spec: dict[str, Any], api_library: APILibrary | None) -> dict[str, Any]:
    prepared=copy.deepcopy(spec)

    def resolve_networks(networks):
        for net in networks:
            if net.get("kind","coil").lower() != "instruction":
                continue
            api_num, definition=resolve_instruction_api(net,api_library)
            net["_resolved_api_num"]=api_num
            if definition is not None:
                net["_api_library"]=api_library.path.name
                net["_api_type"]=definition.api_type
                net["_api_code_template"]=definition.code_template

    programs=normalize_programs(prepared)
    for program in programs:
        resolve_networks(program.get("networks",[]))

    function_blocks=normalize_function_blocks(prepared)
    for fb in function_blocks:
        resolve_networks(fb.get("networks",[]))

    if "programs" in prepared:
        prepared["programs"]=programs
    else:
        prepared["pou"]=programs[0]["name"]
        prepared["pou_comment"]=programs[0].get("comment","")
        prepared["networks"]=programs[0].get("networks",[])

    prepared["function_blocks"]=function_blocks
    return prepared



def replace_all_pous_v24(
    text: str,
    programs: list[dict[str,Any]],
    function_blocks: list[dict[str,Any]],
) -> str:
    matches=list(re.finditer(r"<POU>.*?</POU>",text,re.S))
    if not matches:
        raise ValueError("La plantilla no contiene ninguna POU.")

    fb_by_name={fb["name"]:fb for fb in function_blocks}
    generated=[]
    for program in programs:
        generated.append(serialize_pou(program,fb_by_name))
    for fb in function_blocks:
        generated.append(serialize_function_block(fb))

    start=matches[0].start()
    end=matches[-1].end()
    return text[:start]+"\n".join(generated)+text[end:]


# =============================================================================
# Validación
# =============================================================================

def _walk_logic(
    expr: dict[str, Any],
    contacts: list[tuple[str, str]],
) -> None:
    typ = expr.get("type")

    if typ == "contact":
        name = expr.get("name")
        kind = expr.get("kind", "NO").upper()

        if not name:
            raise ValueError("Contacto sin nombre.")

        if kind not in CONTACT_TYPES:
            raise ValueError(
                f"Tipo de contacto no soportado: {kind}"
            )

        contacts.append((name, kind))
        return

    if typ == "series":
        items = expr.get("items", [])
        if not items:
            raise ValueError("Serie vacía.")

        for item in items:
            _walk_logic(item, contacts)
        return

    if typ == "parallel":
        branches = expr.get("branches", [])
        if len(branches) < 2:
            raise ValueError(
                "Un paralelo necesita al menos dos ramas."
            )

        for branch in branches:
            _walk_logic(branch, contacts)
        return

    raise ValueError(
        f"Tipo de expresión lógica no soportado: {typ}"
    )


def validate_spec(spec: dict[str, Any]) -> list[str]:
    warnings: list[str] = []

    variables = spec.get("variables", [])
    programs = normalize_programs(spec)

    # Variables duplicadas.
    names = [var.get("name") for var in variables]
    duplicates = sorted(
        {name for name in names if names.count(name) > 1}
    )
    if duplicates:
        raise ValueError(
            "Variables globales duplicadas: "
            + ", ".join(duplicates)
        )

    # Direcciones duplicadas: advertencia, no error.
    devices = [var.get("device") for var in variables]
    duplicate_devices = sorted(
        {
            device
            for device in devices
            if device and devices.count(device) > 1
        }
    )
    if duplicate_devices:
        warnings.append(
            "Direcciones utilizadas por más de una variable: "
            + ", ".join(duplicate_devices)
        )

    # Programas duplicados.
    program_names = [program.get("name") for program in programs]
    if any(not name for name in program_names):
        raise ValueError("Hay un programa sin nombre.")

    duplicate_programs = sorted(
        {
            name
            for name in program_names
            if program_names.count(name) > 1
        }
    )
    if duplicate_programs:
        raise ValueError(
            "Programas duplicados: "
            + ", ".join(duplicate_programs)
        )

    written_outputs: dict[str, list[str]] = {}

    for program in programs:
        pname = program["name"]
        networks = normalize_network_ids(
            program.get("networks", [])
        )

        if not networks:
            raise ValueError(
                f"El programa {pname!r} no tiene Networks."
            )

        ids = [network["id"] for network in networks]
        if len(ids) != len(set(ids)):
            raise ValueError(
                f"NET_ID duplicado dentro de {pname}."
            )

        for network in networks:
            kind = network.get("kind", "coil").lower()

            if kind == "coil":
                contacts: list[tuple[str, str]] = []
                _walk_logic(network["logic"], contacts)

                coil_data = network.get("coil", {})
                output_name = coil_data.get("name")
                coil_kind = coil_data.get(
                    "kind", "NORMAL"
                ).upper()

                if not output_name:
                    raise ValueError(
                        f"Bobina sin nombre en {pname}, "
                        f"Network {network['id']}."
                    )

                if coil_kind not in COIL_TYPES:
                    raise ValueError(
                        f"Tipo de bobina no soportado: {coil_kind}"
                    )

                written_outputs.setdefault(
                    output_name, []
                ).append(
                    f"{pname}/N{network['id']}"
                )

            elif kind == "instruction":
                contacts = []
                _walk_logic(network["enable"], contacts)

                if "symbol" not in network:
                    raise ValueError(
                        f"Falta 'symbol' en "
                        f"{pname}/N{network['id']}."
                    )

            elif kind == "comparator":
                contacts = []
                _walk_logic(network["enable"], contacts)

                if "symbol" not in network:
                    raise ValueError(
                        f"Comparador sin símbolo en "
                        f"{pname}/N{network['id']}."
                    )

            elif kind == "fb_call":
                # Se valida en validate_fb_spec.
                pass
            else:
                raise ValueError(
                    f"Tipo de Network no soportado: {kind}"
                )

    # Una variable escrita varias veces puede ser intencional (SET/RST,
    # secuencias), por eso se informa como warning.
    for output, locations in written_outputs.items():
        if len(locations) > 1:
            warnings.append(
                f"{output} se escribe en varias Networks: "
                + ", ".join(locations)
            )

    return warnings



def validate_fb_spec(spec: dict[str,Any]) -> list[str]:
    warnings=[]
    fbs=normalize_function_blocks(spec)
    fb_by_name={fb["name"]:fb for fb in fbs}

    for fb in fbs:
        names=[v.get("name") for v in fb.get("variables",[])]
        dup=sorted({n for n in names if names.count(n)>1})
        if dup:
            raise ValueError(
                f"FB {fb['name']}: variables duplicadas: "+", ".join(dup)
            )
        for v in fb.get("variables",[]):
            scope=v.get("scope","VAR").upper()
            if scope not in FB_VAR_SCOPES:
                raise ValueError(
                    f"FB {fb['name']} / {v.get('name')}: scope inválido {scope}."
                )
            _fb_storage_class(v.get("data_type","BOOL"))

    instances=collect_fb_instances(spec,fb_by_name)
    instance_names=[i["name"] for i in instances]
    dup=sorted({n for n in instance_names if instance_names.count(n)>1})
    if dup:
        raise ValueError("Instancias FB duplicadas: "+", ".join(dup))

    declared={i["name"]:i for i in instances}
    for program in normalize_programs(spec):
        for net in normalize_network_ids(program.get("networks",[])):
            if net.get("kind","").lower()!="fb_call":
                continue
            fb_name=net.get("fb") or net.get("symbol")
            instance=net.get("instance")
            if instance not in declared:
                raise ValueError(
                    f"{program['name']}/N{net['id']}: instancia {instance!r} "
                    "no está declarada como variable global fb_type."
                )
            if declared[instance]["fb_type"]!=fb_name:
                raise ValueError(
                    f"{instance} es {declared[instance]['fb_type']}, "
                    f"pero la llamada usa {fb_name}."
                )
            # serialize function performs exact signature count validation.
            serialize_fb_call_network(net,fb_by_name)

    return warnings


# =============================================================================
# Construcción del proyecto
# =============================================================================

def build_project(
    template_path: str | Path,
    spec: dict[str, Any],
    output_path: str | Path,
    api_library: APILibrary | None = None,
    special_library: SpecialDeviceLibrary | None = None,
    knowledge: KnowledgeBase | None = None,
) -> Path:
    spec = prepare_spec_for_special_devices(spec, special_library)
    spec = prepare_spec_for_api_library(spec, api_library)
    warnings = validate_spec(spec)
    warnings.extend(validate_fb_spec(spec))
    warnings.extend(validate_special_device_usage(spec, special_library))
    warnings.extend(validate_with_knowledge(spec, knowledge, api_library))

    container = ISPContainer.load(template_path)
    encoding = spec.get("encoding", "cp1252")
    text = container.payload.decode(encoding)

    # Variables globales.
    variables = serialize_global_variables_v24(
        spec.get("variables", [])
    )
    text, count = re.subn(
        r"<GLOBAL_VAR>.*?</GLOBAL_VAR>",
        variables,
        text,
        count=1,
        flags=re.S,
    )
    if count != 1:
        raise ValueError(
            "No se pudo reemplazar GLOBAL_VAR."
        )

    programs = normalize_programs(spec)
    program_names = [program["name"] for program in programs]
    function_blocks = normalize_function_blocks(spec)
    fb_names = [fb["name"] for fb in function_blocks]

    # Tarea cíclica y orden de ejecución.
    task_text = serialize_task(
        program_names,
        spec.get("task"),
    )
    text, count = re.subn(
        r"<TASK>.*?</TASK>",
        task_text,
        text,
        count=1,
        flags=re.S,
    )
    if count != 1:
        raise ValueError("No se pudo reemplazar TASK.")

    # Árbol de programas.
    folder_text = serialize_group_pou_folder_v24(
        program_names, fb_names
    )
    text, count = re.subn(
        r"<GroupPOUFolder>.*?</GroupPOUFolder>",
        folder_text,
        text,
        count=1,
        flags=re.S,
    )
    if count != 1:
        raise ValueError(
            "No se pudo reemplazar GroupPOUFolder."
        )

    # Programas + Function Blocks.
    text = replace_all_pous_v24(
        text, programs, function_blocks
    )

    # Asignación automática de memoria interna de instancias FB.
    fbinst_lines, fb_alloc = allocate_fb_instances(
        spec, text, function_blocks
    )
    fbinst_text = serialize_fb_instances_section(fbinst_lines)

    if re.search(r"<FBInst>.*?</FBInst>", text, re.S):
        text = re.sub(
            r"<FBInst>.*?</FBInst>",
            fbinst_text,
            text,
            count=1,
            flags=re.S,
        )
    else:
        marker = "<ARY_VAR>"
        if marker not in text:
            raise ValueError("No se encontró <ARY_VAR> para insertar <FBInst>.")
        text = text.replace(marker, fbinst_text+"\n"+marker, 1)

    new_payload = text.encode(encoding)
    rebuilt = container.rebuild(new_payload)

    output = Path(output_path)
    output.write_bytes(rebuilt)

    # Verificación de integridad del contenedor recién generado.
    check = ISPContainer.load(output)
    if check.payload != new_payload:
        raise RuntimeError(
            "La verificación del .isp generado falló."
        )

    # Verificación estructural mínima.
    check_text = check.payload.decode(encoding)
    generated_names = re.findall(
        r"^P_Name=(.+)$",
        check_text,
        re.M,
    )

    expected_pous = program_names + fb_names
    if generated_names[: len(expected_pous)] != expected_pous:
        raise RuntimeError(
            "El orden de POUs generado no coincide con la especificación."
        )

    # Se dejan warnings accesibles para uso como librería.
    build_project.last_warnings = warnings

    return output


build_project.last_warnings = []


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generador de proyectos ISPSoft .isp "
            f"(v{VERSION})"
        )
    )
    parser.add_argument(
        "template",
        help="Proyecto .isp base creado por ISPSoft",
    )
    parser.add_argument(
        "spec",
        help="Archivo JSON con variables y programas",
    )
    parser.add_argument(
        "output",
        help="Archivo .isp de salida",
    )
    parser.add_argument(
        "--api",
        help=(
            "Librería de instrucciones ISPSoft (*.api), por ejemplo SE.api. "
            "Tiene prioridad sobre plc.api_library del JSON."
        ),
    )
    parser.add_argument(
        "--special-devices",
        help="Catálogo Special M/D específico de CPU, por ejemplo DVP_SE_special_devices.json.",
    )
    parser.add_argument(
        "--knowledge",
        help=(
            "Base cruzada de conocimiento de CPU/instrucciones, por ejemplo "
            "DVP_SE_full_knowledge.json."
        ),
    )
    parser.add_argument(
        "--list-special",
        nargs="?",
        const="",
        metavar="FILTER",
        help="Lista Special M/D; opcionalmente filtra por alias, dirección o descripción.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Valida el JSON sin generar un .isp",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=VERSION,
    )

    args = parser.parse_args()

    spec_path = Path(args.spec)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    api_path = resolve_api_library_path(
        spec, spec_path=spec_path, cli_api=args.api
    )
    api_library = APILibrary(api_path) if api_path is not None else None

    special_path = resolve_special_library_path(
        spec, spec_path=spec_path, cli_special=args.special_devices
    )
    special_library = (
        SpecialDeviceLibrary(special_path) if special_path is not None else None
    )

    knowledge_path = resolve_knowledge_path(
        spec, spec_path=spec_path, cli_knowledge=args.knowledge
    )
    knowledge = (
        KnowledgeBase(knowledge_path) if knowledge_path is not None else None
    )

    if args.list_special is not None:
        if special_library is None:
            raise ValueError("--list-special requiere --special-devices o plc.special_devices_library.")
        filt=args.list_special.upper()
        aliases_by_address={}
        for alias,address in special_library.aliases.items():
            aliases_by_address.setdefault(address,[]).append(alias)
        for address,dev in sorted(special_library.devices.items(), key=lambda kv:(kv[0][0],int(kv[0][1:]))):
            aliases=", ".join(aliases_by_address.get(address,[]))
            hay=f"{address} {aliases} {dev.description}".upper()
            if filt and filt not in hay:
                continue
            print(f"{address}" + (f" [{aliases}]" if aliases else "") + f": {dev.description} (access={dev.access}, latched={dev.latched}, default={dev.default})")
        return

    prepared = prepare_spec_for_special_devices(spec, special_library)
    prepared = prepare_spec_for_api_library(prepared, api_library)
    warnings = validate_spec(prepared)
    warnings.extend(validate_fb_spec(prepared))
    warnings.extend(validate_special_device_usage(prepared, special_library))
    warnings.extend(validate_with_knowledge(prepared, knowledge, api_library))

    if api_library is not None:
        print(f"Librería API: {api_library.path} ({len(api_library)} instrucciones)")
    if special_library is not None:
        print(f"Special M/D: {special_library.path} ({len(special_library)} dispositivos, CPU={special_library.model})")
    if knowledge is not None:
        print(
            f"Knowledge: {knowledge.path} ({len(knowledge)} instrucciones, "
            f"CPU={knowledge.cpu_profile.get('model','?')})"
        )

    if args.validate_only:
        print("Especificación válida.")
        for warning in warnings:
            print(f"ADVERTENCIA: {warning}")
        return

    output = build_project(
        args.template, spec, args.output,
        api_library=api_library,
        special_library=special_library,
        knowledge=knowledge
    )

    print(f"Generado correctamente: {output}")

    for warning in build_project.last_warnings:
        print(f"ADVERTENCIA: {warning}")


if __name__ == "__main__":
    main()
