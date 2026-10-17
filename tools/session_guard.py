"""Task envelope and checkpoint guard for long-running Codex sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from . import check_repo
except ImportError:  # Direct execution: python tools/session_guard.py
    import check_repo


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 2

ENVELOPE_FIELDS = (
    "schema_version",
    "task_id",
    "baseline_commit",
    "policy_epoch_algorithm",
    "policy_epoch",
    "mode",
    "role",
    "owner",
    "authority_source",
    "allowed_read_paths",
    "allowed_write_paths",
    "protected_paths",
    "preexisting_dirty_paths",
    "commit_policy",
    "push_authorized",
    "acceptance_checks",
)

CHECKPOINT_FIELDS = (
    "schema_version",
    "task_id",
    "sequence",
    "envelope_sha256",
    "policy_epoch_algorithm",
    "policy_epoch",
    "repo_head",
    "worktree_status_sha256",
    "completed_gate",
    "evidence",
    "open_items",
    "assumptions",
    "next_step",
)


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ("git", "-C", str(root), *args),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON kökü object olmalı")
    return value


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_head(root: Path) -> str:
    result = _run_git(root, "rev-parse", "HEAD")
    if result.returncode:
        return ""
    return result.stdout.decode("ascii").strip()


def worktree_status_bytes(root: Path) -> bytes:
    result = _run_git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    return result.stdout if result.returncode == 0 else b""


def worktree_status_sha256(root: Path) -> str:
    return hashlib.sha256(worktree_status_bytes(root)).hexdigest()


def worktree_paths(root: Path) -> list[str]:
    parts = worktree_status_bytes(root).split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(parts):
        entry = parts[index]
        if not entry:
            index += 1
            continue
        status = entry[:2]
        raw_path = entry[3:]
        paths.append(raw_path.decode("utf-8").replace("\\", "/"))
        index += 1
        if status[:1] in {b"R", b"C"} or status[1:2] in {b"R", b"C"}:
            if index < len(parts) and parts[index]:
                paths.append(parts[index].decode("utf-8").replace("\\", "/"))
            index += 1
    return sorted(set(paths))


def staged_paths(root: Path) -> list[str]:
    result = _run_git(root, "diff", "--cached", "--name-only", "-z")
    if result.returncode:
        return []
    return sorted(
        {
            raw.decode("utf-8").replace("\\", "/")
            for raw in result.stdout.split(b"\0")
            if raw
        }
    )


def normalize_repo_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    pure = PurePosixPath(normalized)
    if not normalized or pure.is_absolute() or ".." in pure.parts or ":" in normalized:
        raise ValueError(f"Repo-relative yol bekleniyordu: {value!r}")
    return pure.as_posix()


def _path_matches(path: str, rule: str) -> bool:
    normalized_path = normalize_repo_path(path)
    normalized_rule = normalize_repo_path(rule.rstrip("/"))
    return normalized_path == normalized_rule or (
        rule.endswith("/") and normalized_path.startswith(normalized_rule + "/")
    )


def _path_allowed(path: str, rules: list[str]) -> bool:
    return any(_path_matches(path, rule) for rule in rules)


def _required_fields(value: dict[str, Any], fields: tuple[str, ...], label: str) -> list[str]:
    return [f"{label} alanı eksik: {field}" for field in fields if field not in value]


def validate_envelope(root: Path, envelope: dict[str, Any]) -> list[str]:
    root = root.resolve()
    errors = _required_fields(envelope, ENVELOPE_FIELDS, "Envelope")
    if errors:
        return errors

    if envelope["schema_version"] != SCHEMA_VERSION:
        errors.append("Envelope schema_version desteklenmiyor")
    if envelope["mode"] not in {"NORMAL", "FAST"}:
        errors.append("Envelope mode NORMAL veya FAST olmalı")
    for field in ("task_id", "role", "owner", "authority_source"):
        if not isinstance(envelope[field], str) or not envelope[field].strip():
            errors.append(f"Envelope {field} boş olamaz")
    for field in (
        "allowed_read_paths",
        "allowed_write_paths",
        "protected_paths",
        "preexisting_dirty_paths",
        "acceptance_checks",
    ):
        if not isinstance(envelope[field], list) or not all(
            isinstance(item, str) and item.strip() for item in envelope[field]
        ):
            errors.append(f"Envelope {field} boş olmayan string listesi olmalı")
    if envelope["commit_policy"] != "SCOPED_MANDATORY":
        errors.append("Envelope commit_policy SCOPED_MANDATORY olmalı")
    if not isinstance(envelope["push_authorized"], bool):
        errors.append("Envelope push_authorized boolean olmalı")

    if envelope["policy_epoch_algorithm"] != check_repo.POLICY_EPOCH_ALGORITHM:
        errors.append("Envelope policy epoch algoritması güncel değil")
    try:
        current_epoch = check_repo.calculate_policy_epoch(root)
    except OSError as exc:
        errors.append(f"Policy epoch hesaplanamadı: {exc}")
    else:
        if envelope["policy_epoch"] != current_epoch:
            errors.append("Envelope policy epoch güncel control plane ile uyuşmuyor")

    errors.extend(check_repo.check_git_control_plane(root))
    baseline = str(envelope["baseline_commit"])
    if _run_git(root, "rev-parse", "--verify", f"{baseline}^{{commit}}").returncode:
        errors.append("Envelope baseline commit doğrulanamadı")
    elif _run_git(root, "merge-base", "--is-ancestor", baseline, "HEAD").returncode:
        errors.append("Envelope baseline mevcut HEAD'in atası değil")

    try:
        for field in (
            "allowed_read_paths",
            "allowed_write_paths",
            "protected_paths",
            "preexisting_dirty_paths",
        ):
            for value in envelope[field]:
                normalize_repo_path(value.rstrip("/"))
    except ValueError as exc:
        errors.append(str(exc))
    return errors


def validate_checkpoint(
    root: Path,
    envelope_path: Path,
    envelope: dict[str, Any],
    checkpoint: dict[str, Any],
) -> list[str]:
    errors = validate_envelope(root, envelope)
    errors.extend(_required_fields(checkpoint, CHECKPOINT_FIELDS, "Checkpoint"))
    if any(error.startswith("Checkpoint alanı eksik") for error in errors):
        return errors

    if checkpoint["schema_version"] != SCHEMA_VERSION:
        errors.append("Checkpoint schema_version desteklenmiyor")
    if checkpoint["task_id"] != envelope["task_id"]:
        errors.append("Checkpoint task_id envelope ile uyuşmuyor")
    if not isinstance(checkpoint["sequence"], int) or checkpoint["sequence"] < 0:
        errors.append("Checkpoint sequence negatif olmayan integer olmalı")
    if checkpoint["envelope_sha256"] != file_sha256(envelope_path):
        errors.append("Checkpoint envelope hash'i güncel dosyayla uyuşmuyor")
    if checkpoint["policy_epoch_algorithm"] != check_repo.POLICY_EPOCH_ALGORITHM:
        errors.append("Checkpoint policy epoch algoritması güncel değil")
    if checkpoint["policy_epoch"] != check_repo.calculate_policy_epoch(root):
        errors.append("Checkpoint policy epoch güncel control plane ile uyuşmuyor")
    if checkpoint["repo_head"] != current_head(root):
        errors.append("Checkpoint repo_head mevcut HEAD ile uyuşmuyor")
    if checkpoint["worktree_status_sha256"] != worktree_status_sha256(root):
        errors.append("Checkpoint worktree özeti güncel değil")
    if not isinstance(checkpoint["next_step"], str) or not checkpoint["next_step"].strip():
        errors.append("Checkpoint next_step boş olamaz")
    for field in ("evidence", "open_items", "assumptions"):
        if not isinstance(checkpoint[field], list):
            errors.append(f"Checkpoint {field} list olmalı")
    return errors


def verify_action(
    root: Path,
    envelope_path: Path,
    checkpoint_path: Path,
    kind: str,
    path: str | None = None,
) -> list[str]:
    envelope = load_json_object(envelope_path)
    checkpoint = load_json_object(checkpoint_path)
    errors = validate_checkpoint(root, envelope_path, envelope, checkpoint)
    if errors:
        return errors

    if kind in {"read", "edit"}:
        if path is None:
            return [f"{kind} için --path zorunlu"]
        try:
            normalized = normalize_repo_path(path)
        except ValueError as exc:
            return [str(exc)]
        if kind == "edit" and _path_allowed(normalized, envelope["protected_paths"]):
            errors.append(f"Korunan yola erişim reddedildi: {normalized}")
        rules = envelope["allowed_read_paths"] if kind == "read" else envelope["allowed_write_paths"]
        if not _path_allowed(normalized, rules):
            errors.append(f"{kind} scope dışında: {normalized}")
        return errors

    if kind == "commit":
        for dirty in worktree_paths(root):
            allowed = _path_allowed(dirty, envelope["allowed_write_paths"])
            preexisting = _path_allowed(dirty, envelope["preexisting_dirty_paths"])
            protected = _path_allowed(dirty, envelope["protected_paths"])
            if protected or not (allowed or preexisting):
                errors.append(f"Commit penceresinde scope dışı dirty yol: {dirty}")
        staged = staged_paths(root)
        if not staged:
            errors.append("Commit penceresinde index boş")
        for staged_path in staged:
            allowed = _path_allowed(staged_path, envelope["allowed_write_paths"])
            protected = _path_allowed(staged_path, envelope["protected_paths"])
            if protected or not allowed:
                errors.append(f"Stage scope dışında: {staged_path}")
        return errors

    if kind == "push" and not envelope["push_authorized"]:
        errors.append("Push/remote/history yetkisi yok")
    return errors


def snapshot(root: Path, envelope_path: Path) -> tuple[dict[str, Any], list[str]]:
    envelope = load_json_object(envelope_path)
    errors = validate_envelope(root, envelope)
    value = {
        "envelope_sha256": file_sha256(envelope_path),
        "policy_epoch_algorithm": check_repo.POLICY_EPOCH_ALGORITHM,
        "policy_epoch": check_repo.calculate_policy_epoch(root),
        "repo_head": current_head(root),
        "worktree_status_sha256": worktree_status_sha256(root),
        "dirty_paths": worktree_paths(root),
    }
    return value, errors


def _print_result(errors: list[str], success: str) -> int:
    if errors:
        print(f"SESSION_GUARD_FAIL ({len(errors)} bulgu)")
        for error in errors:
            print(f"- {error}")
        return 1
    print(success)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    envelope_parser = subparsers.add_parser("envelope")
    envelope_parser.add_argument("--envelope", type=Path, required=True)

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--envelope", type=Path, required=True)
    resume_parser.add_argument("--checkpoint", type=Path, required=True)

    action_parser = subparsers.add_parser("action")
    action_parser.add_argument("--envelope", type=Path, required=True)
    action_parser.add_argument("--checkpoint", type=Path, required=True)
    action_parser.add_argument("--kind", choices=("read", "edit", "commit", "push"), required=True)
    action_parser.add_argument("--path")

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--envelope", type=Path, required=True)

    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "envelope":
            errors = validate_envelope(root, load_json_object(args.envelope))
            return _print_result(errors, "SESSION_GUARD_ENVELOPE_OK")
        if args.command == "resume":
            envelope = load_json_object(args.envelope)
            checkpoint = load_json_object(args.checkpoint)
            errors = validate_checkpoint(root, args.envelope, envelope, checkpoint)
            return _print_result(errors, "SESSION_GUARD_RESUME_OK")
        if args.command == "action":
            errors = verify_action(
                root,
                args.envelope,
                args.checkpoint,
                args.kind,
                args.path,
            )
            return _print_result(errors, f"SESSION_GUARD_ACTION_OK kind={args.kind}")
        value, errors = snapshot(root, args.envelope)
        if errors:
            return _print_result(errors, "")
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _print_result([str(exc)], "")


if __name__ == "__main__":
    sys.exit(main())
