from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_UTC_RE = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-([0-2][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$"
)
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ProtocolViolation(ValueError):
    """Fail-closed input or protocol violation."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        raise ProtocolViolation("timestamp must be UTC seconds precision")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ProtocolViolation("invalid UTC timestamp") from exc
    return parsed


def validate_digest(value: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ProtocolViolation("digest must be sha256 plus 64 lowercase hex characters")
    return value


def validate_correlation_id(value: str) -> str:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise ProtocolViolation("correlation_id must be a canonical lowercase UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ProtocolViolation("invalid correlation_id") from exc
    if str(parsed) != value:
        raise ProtocolViolation("correlation_id is not canonical")
    return value


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _duplicate_rejector(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolViolation(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _number_rejector(value: str) -> Any:
    raise ProtocolViolation(f"JSON numbers are forbidden by AXQ-FILE/1: {value}")


def strict_json_loads(data: bytes | str) -> dict[str, Any]:
    if isinstance(data, bytes):
        if data.startswith(b"\xef\xbb\xbf"):
            raise ProtocolViolation("UTF-8 BOM is forbidden")
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProtocolViolation("invalid UTF-8") from exc
    elif isinstance(data, str):
        text = data
    else:
        raise ProtocolViolation("JSON input must be bytes or text")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_rejector,
            parse_int=_number_rejector,
            parse_float=_number_rejector,
            parse_constant=_number_rejector,
        )
    except ProtocolViolation:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ProtocolViolation("invalid JSON object") from exc
    if not isinstance(value, dict):
        raise ProtocolViolation("top-level JSON value must be an object")
    return value


def strict_json_loads_v2(data: bytes | str) -> dict[str, Any]:
    """Load an AXQ-FILE/2 object while rejecting ambiguous JSON constructs.

    V2 needs numeric fields for revisions, timing and typed observation
    invariants. Non-finite constants, duplicate keys, BOMs and non-object
    top-level values remain fail-closed; operation validators decide where a
    finite float is permitted.
    """

    if isinstance(data, bytes):
        if data.startswith(b"\xef\xbb\xbf"):
            raise ProtocolViolation("UTF-8 BOM is forbidden")
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProtocolViolation("invalid UTF-8") from exc
    elif isinstance(data, str):
        text = data
    else:
        raise ProtocolViolation("JSON input must be bytes or text")

    def reject_constant(value: str) -> Any:
        raise ProtocolViolation(f"non-finite JSON number is forbidden by AXQ-FILE/2: {value}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_rejector,
            parse_constant=reject_constant,
        )
    except ProtocolViolation:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ProtocolViolation("invalid JSON object") from exc
    if not isinstance(value, dict):
        raise ProtocolViolation("top-level JSON value must be an object")
    ensure_json_safe_v2(value)
    return value


def ensure_json_safe_v2(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if value < -(2**63) or value > 2**63 - 1:
            raise ProtocolViolation("AXQ-FILE/2 integer is outside signed 64-bit range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolViolation("AXQ-FILE/2 non-finite numbers are forbidden")
        return
    if isinstance(value, list):
        for item in value:
            ensure_json_safe_v2(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolViolation("JSON object keys must be strings")
            ensure_json_safe_v2(item)
        return
    raise ProtocolViolation(f"unsupported JSON value type: {type(value).__name__}")


def ensure_json_safe(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ProtocolViolation("non-finite number")
        raise ProtocolViolation("AXQ-FILE/1 records cannot contain JSON numbers")
    if isinstance(value, list):
        for item in value:
            ensure_json_safe(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolViolation("JSON object keys must be strings")
            ensure_json_safe(item)
        return
    raise ProtocolViolation(f"unsupported JSON value type: {type(value).__name__}")


def validate_artifact_path(value: str, scratch_root: Path | None = None) -> Path:
    if not isinstance(value, str) or not value:
        raise ProtocolViolation("artifact path must be non-empty text")
    if "\\" in value or ":" in value or "\x00" in value:
        raise ProtocolViolation("backslash, drive/device and NUL paths are forbidden")
    if value.startswith("/") or value.startswith("//"):
        raise ProtocolViolation("absolute and UNC paths are forbidden")
    decoded = unquote(value)
    if decoded != value or "%" in value:
        raise ProtocolViolation("percent-encoded paths are forbidden")
    segments = value.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ProtocolViolation("empty, dot and traversal segments are forbidden")
    if any(not _SEGMENT_RE.fullmatch(segment) for segment in segments):
        raise ProtocolViolation("artifact path contains a forbidden segment")
    relative = Path(*segments)
    if scratch_root is None:
        return relative
    root = scratch_root.resolve(strict=False)
    target = (root / relative).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ProtocolViolation("artifact path escapes scratch root") from exc
    current = root
    for segment in segments:
        current = current / segment
        if current.exists() and current.is_symlink():
            raise ProtocolViolation("symlink artifact path is forbidden")
    return target


def atomic_write_exclusive(final_path: Path, data: bytes) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        raise ProtocolViolation(f"final file already exists: {final_path.name}")
    partial_path = final_path.with_name(final_path.name + ".partial")
    if partial_path.exists():
        raise ProtocolViolation(f"partial file already exists: {partial_path.name}")
    try:
        with partial_path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(partial_path, final_path)
        except FileExistsError as exc:
            raise ProtocolViolation(
                f"final file appeared during publish: {final_path.name}"
            ) from exc
        except OSError as exc:
            raise ProtocolViolation(
                f"exclusive atomic publish is unavailable: {final_path.name}"
            ) from exc
        partial_path.unlink()
    except Exception:
        try:
            partial_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise


def atomic_write_replace(final_path: Path, data: bytes) -> None:
    """Durably replace task-local state through a same-directory partial file."""

    final_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = final_path.with_name(final_path.name + ".partial")
    if partial_path.exists():
        raise ProtocolViolation(f"partial file already exists: {partial_path.name}")
    try:
        with partial_path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial_path, final_path)
    except Exception:
        try:
            partial_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise


def private_temp_root(prefix: str = "axmcp-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix)).resolve()
