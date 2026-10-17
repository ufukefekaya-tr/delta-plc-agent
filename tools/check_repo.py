"""Universal PLC Agents repo sözleşmesini yalnız Python stdlib ile denetler."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from collections import deque
from pathlib import Path
from urllib.parse import unquote


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE_FILES = ("AGENTS.md", "SYSTEM_PROMPT.md", "INDEX.md")
POLICY_EPOCH_ALGORITHM = "sha256-path-nul-bytes-nul-v1"

CONTRACT_PREFIXES = (
    "src/",
    "backends/",
    "catalog/",
    "goals/",
    "workflows/",
    "reports/",
    "docs/",
    "kaizen/",
    "knowledge/",
    "projects/",
    "prompts/",
    "templates/",
    "tests/",
    "tools/",
)
CONTRACT_TOP_LEVEL = {
    "THIRD_PARTY_NOTICES.md",
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "INDEX.md",
    "README.md",
    "SYSTEM_PROMPT.md",
}

REQUIRED_DIRECTORIES = (
    "docs",
    "kaizen",
    "knowledge",
    "prompts/modes",
    "prompts/roles",
    "templates",
    "tests",
    "tools",
)

REQUIRED_FILES = (
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "INDEX.md",
    "README.md",
    "SYSTEM_PROMPT.md",
    "docs/ENGINEERING_RULES.md",
    "docs/EVIDENCE_AND_HUMAN_BOUNDARY.md",
    "docs/KAIZEN_PROTOCOL.md",
    "docs/TARGET_PROFILE_GUIDE.md",
    "docs/WORKFLOW.md",
    "knowledge/CULTURE_TRANSFER.md",
    "prompts/modes/FAST.md",
    "prompts/roles/ANALYST.md",
    "prompts/roles/ARCHITECT.md",
    "prompts/roles/KAIZEN_CURATOR.md",
    "prompts/roles/PROGRAMMER.md",
    "prompts/roles/TENTH_MAN.md",
    "prompts/roles/VERIFIER.md",
    "templates/ADR.md",
    "templates/CONTROL_CONTRACT.md",
    "templates/HUMAN_HANDOFF.md",
    "templates/IO_AND_VARIABLES.md",
    "templates/KAIZEN.md",
    "templates/LANGUAGE_DECISION.md",
    "templates/LD_NETWORKS.md",
    "templates/PROJECT_BRIEF.md",
    "templates/REVIEW.md",
    "templates/SESSION_CHECKPOINT.md",
    "templates/SESSION_CHECKPOINT.json",
    "templates/ST_MODULE.md",
    "templates/SURGICAL_CHANGE.md",
    "templates/TASK_ENVELOPE.md",
    "templates/TASK_ENVELOPE.json",
    "templates/TARGET_PROFILE.md",
    "templates/TEST_PLAN.md",
    "tests/PROMPT_SCENARIOS.md",
    "tests/__init__.py",
    "tests/test_check_repo.py",
    "tests/test_session_guard.py",
    "tools/check_repo.py",
    "tools/session_guard.py",
)

ROLE_FILES = (
    "prompts/roles/ANALYST.md",
    "prompts/roles/ARCHITECT.md",
    "prompts/roles/PROGRAMMER.md",
    "prompts/roles/VERIFIER.md",
    "prompts/roles/TENTH_MAN.md",
    "prompts/roles/KAIZEN_CURATOR.md",
)

ROLE_HEADINGS = (
    "Amaç",
    "Okunacaklar",
    "Yapılacaklar",
    "Yapılmayacaklar",
    "Çıktı sözleşmesi",
)

SCENARIO_IDS = tuple(f"S{number:02d}" for number in range(1, 32))
TEXT_SUFFIXES = {".json", ".md", ".py", ".txt"}
TEXT_NAMES = {".gitattributes", ".gitignore"}
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\n]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _is_contract_path(relative: Path) -> bool:
    value = relative.as_posix()
    return value in CONTRACT_TOP_LEVEL or value.startswith(CONTRACT_PREFIXES)


def _repo_files(root: Path):
    """Yield contract files, without traversing ignored/vendor worktrees."""

    seen: set[Path] = set()
    tracked = _run_git(root, "ls-files", "-z")
    if tracked.returncode == 0:
        for raw_relative in tracked.stdout.split(b"\0"):
            if not raw_relative:
                continue
            try:
                relative = Path(raw_relative.decode("utf-8"))
            except UnicodeDecodeError:
                continue
            path = root / relative
            if path.is_file() and _is_contract_path(relative):
                seen.add(path.resolve())
                yield path

        # Required files are checked before their first commit as well.
        for value in REQUIRED_FILES:
            path = root / value
            resolved = path.resolve()
            if path.is_file() and resolved not in seen:
                seen.add(resolved)
                yield path
        return

    # Unit-test fixtures and exported snapshots may not be Git repositories.
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if ".git" in relative.parts or "__pycache__" in relative.parts:
            continue
        yield path


def _markdown_files(root: Path):
    yielded: set[Path] = set()
    for path in _repo_files(root):
        if path.suffix.lower() == ".md":
            resolved = path.resolve()
            yielded.add(resolved)
            yield path
    kaizen_root = root / "kaizen"
    if kaizen_root.is_dir():
        for path in sorted(kaizen_root.glob("KZ-*.md")):
            if path.resolve() not in yielded:
                yield path


def _text_files(root: Path):
    for path in _repo_files(root):
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            yield path


def _read_utf8(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def calculate_policy_epoch(root: Path) -> str:
    """Hash path + NUL + raw bytes + NUL in CONTROL_PLANE_FILES order."""

    digest = hashlib.sha256()
    for relative in CONTROL_PLANE_FILES:
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ("git", "-C", str(root), *args),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def check_git_control_plane(root: Path) -> list[str]:
    """Control plane'in tracked, committed ve HEAD ile byte-eş olduğunu denetler."""

    root = root.resolve()
    errors: list[str] = []
    top = _run_git(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return ["Bootstrap Git doğrulaması yapılamadı: repo bulunamadı"]
    try:
        git_root = Path(top.stdout.decode("utf-8").strip()).resolve()
    except UnicodeDecodeError:
        return ["Bootstrap Git doğrulaması yapılamadı: repo yolu UTF-8 değil"]
    if git_root != root:
        errors.append(f"Denetlenen kök Git root değil: {root}")

    if _run_git(root, "rev-parse", "--verify", "HEAD").returncode != 0:
        errors.append("HEAD commit doğrulanamadı")
        return errors

    for relative in CONTROL_PLANE_FILES:
        path = root / relative
        if _run_git(root, "ls-files", "--error-unmatch", "--", relative).returncode:
            errors.append(f"Control plane tracked değil: {relative}")
            continue
        head_blob = _run_git(root, "rev-parse", f"HEAD:{relative}")
        if head_blob.returncode != 0:
            errors.append(f"Control plane HEAD blobu yok: {relative}")
            continue
        if not path.is_file():
            errors.append(f"Control plane çalışma dosyası okunamadı: {relative}")
            continue
        working_blob = _run_git(
            root, "hash-object", "--no-filters", "--", relative
        )
        if working_blob.returncode != 0:
            errors.append(f"Control plane çalışma hash'i alınamadı: {relative}")
            continue
        if head_blob.stdout.strip() != working_blob.stdout.strip():
            errors.append(f"Control plane HEAD ile aynı değil: {relative}")
        if _run_git(root, "diff", "--quiet", "--", relative).returncode:
            errors.append(f"Control plane unstaged değişiklik taşıyor: {relative}")
        if _run_git(root, "diff", "--cached", "--quiet", "--", relative).returncode:
            errors.append(f"Control plane staged değişiklik taşıyor: {relative}")
    return errors


def check_structure(root: Path) -> list[str]:
    errors: list[str] = []
    for relative in REQUIRED_DIRECTORIES:
        if not (root / relative).is_dir():
            errors.append(f"Eksik dizin: {relative}")
    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            errors.append(f"Eksik dosya: {relative}")
    return errors


def check_text_format(root: Path) -> list[str]:
    errors: list[str] = []
    for path in _text_files(root):
        relative = _display(path, root)
        data = path.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            errors.append(f"UTF-8 BOM kullanılmamalı: {relative}")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"UTF-8 değil: {relative} ({exc})")
            continue
        if b"\r" in data:
            errors.append(f"LF dışında satır sonu var: {relative}")
        if data and not data.endswith(b"\n"):
            errors.append(f"Dosya sonunda LF yok: {relative}")
    return errors


def _visible_markdown_lines(text: str):
    in_fence = False
    fence_marker = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            yield ""
            continue
        yield "" if in_fence else line


def _markdown_links(text: str) -> list[str]:
    visible = "\n".join(_visible_markdown_lines(text))
    visible = re.sub(r"\x60[^\x60\n]*\x60", "", visible)
    return [match.group(1).strip() for match in MARKDOWN_LINK.finditer(visible)]


def _split_target(raw_target: str) -> tuple[str, str] | None:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    else:
        target = target.split(maxsplit=1)[0]
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith("//"):
        return None
    path_part, separator, anchor = target.partition("#")
    path_part = unquote(path_part.split("?", 1)[0])
    return path_part, unquote(anchor) if separator else ""


def _resolve_target(source: Path, root: Path, path_part: str) -> Path | None:
    candidate = source if not path_part else source.parent / path_part
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _slugify_heading(value: str) -> str:
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace(chr(96), "").lower()
    value = "".join(
        character
        for character in value
        if character.isalnum() or character in {" ", "-", "_"}
    )
    return re.sub(r"\s+", "-", value.strip())


def _heading_anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in _visible_markdown_lines(text):
        match = HEADING.match(line)
        if not match:
            continue
        base = _slugify_heading(match.group(2))
        occurrence = counts.get(base, 0)
        anchor = base if occurrence == 0 else f"{base}-{occurrence}"
        counts[base] = occurrence + 1
        anchors.add(anchor)
    return anchors


def check_markdown_links(root: Path) -> list[str]:
    errors: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}
    for source in sorted(_markdown_files(root)):
        try:
            text = _read_utf8(source)
        except UnicodeDecodeError:
            continue
        for raw_target in _markdown_links(text):
            parts = _split_target(raw_target)
            if parts is None:
                continue
            path_part, anchor = parts
            target = _resolve_target(source, root, path_part)
            label = f"{_display(source, root)} -> {raw_target}"
            if target is None:
                errors.append(f"Repo dışına çıkan bağlantı: {label}")
                continue
            if not target.exists():
                errors.append(f"Kırık bağlantı: {label}")
                continue
            if anchor and target.is_file() and target.suffix.lower() == ".md":
                if target not in anchor_cache:
                    try:
                        anchor_cache[target] = _heading_anchors(_read_utf8(target))
                    except UnicodeDecodeError:
                        continue
                if anchor.lower() not in anchor_cache[target]:
                    errors.append(f"Kırık anchor: {label}")
    return errors


def _markdown_graph(root: Path) -> dict[Path, set[Path]]:
    graph: dict[Path, set[Path]] = {}
    for source in _markdown_files(root):
        source = source.resolve()
        graph[source] = set()
        try:
            links = _markdown_links(_read_utf8(source))
        except UnicodeDecodeError:
            continue
        for raw_target in links:
            parts = _split_target(raw_target)
            if parts is None:
                continue
            target = _resolve_target(source, root, parts[0])
            if target and target.is_file() and target.suffix.lower() == ".md":
                graph[source].add(target)
    return graph


def check_index_reachability(root: Path) -> list[str]:
    index = (root / "INDEX.md").resolve()
    if not index.is_file():
        return ["INDEX.md erişilebilirlik denetimi yapılamadı"]
    graph = _markdown_graph(root)
    visited: set[Path] = set()
    queue: deque[Path] = deque([index])
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        queue.extend(graph.get(current, set()) - visited)
    all_markdown = set(graph)
    return [
        f"INDEX.md üzerinden erişilemiyor: {_display(path, root)}"
        for path in sorted(all_markdown - visited)
    ]


def check_role_cards(root: Path) -> list[str]:
    errors: list[str] = []
    for relative in ROLE_FILES:
        path = root / relative
        if not path.is_file():
            continue
        try:
            text = _read_utf8(path)
        except UnicodeDecodeError:
            continue
        headings = {
            match.group(1).strip()
            for match in re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE)
        }
        for required in ROLE_HEADINGS:
            if required not in headings:
                errors.append(f"Eksik rol başlığı: {relative} -> {required}")
        if len(text.splitlines()) > 80:
            errors.append(f"Rol kartı 80 satırı aşıyor: {relative}")
        if "../../SYSTEM_PROMPT.md" not in text:
            errors.append(f"Rol kartı kanonik prompta bağlanmıyor: {relative}")
    return errors


def check_prompt_contract(root: Path) -> list[str]:
    errors: list[str] = []
    prompt = root / "SYSTEM_PROMPT.md"
    if prompt.is_file():
        try:
            line_count = len(_read_utf8(prompt).splitlines())
        except UnicodeDecodeError:
            line_count = 0
        if line_count > 250:
            errors.append(f"SYSTEM_PROMPT.md 250 satırı aşıyor: {line_count}")

    scenarios = root / "tests/PROMPT_SCENARIOS.md"
    if scenarios.is_file():
        try:
            text = _read_utf8(scenarios)
        except UnicodeDecodeError:
            return errors
        found = set(re.findall(r"^##\s+(S\d{2})\b", text, re.MULTILINE))
        for scenario_id in SCENARIO_IDS:
            if scenario_id not in found:
                errors.append(f"Eksik prompt senaryosu: {scenario_id}")
    return errors


def check_bootstrap_contract(root: Path) -> list[str]:
    errors: list[str] = []
    required_tokens = (
        "BOOTSTRAP_ACK",
        "POLICY_EPOCH",
        POLICY_EPOCH_ALGORITHM,
        "RESUME_ACK",
        "SESSION_CHECKPOINT.md",
        "SESSION_CHECKPOINT.json",
        "TASK_ENVELOPE.json",
        "session_guard.py",
        "SCOPED_MANDATORY",
    )
    for relative in ("AGENTS.md", "SYSTEM_PROMPT.md"):
        path = root / relative
        if not path.is_file():
            continue
        try:
            text = _read_utf8(path)
        except UnicodeDecodeError:
            continue
        for token in required_tokens:
            if token not in text:
                errors.append(f"Bootstrap sözleşmesi eksik: {relative} -> {token}")
    return errors


def check_kaizen_records(root: Path) -> list[str]:
    errors: list[str] = []
    kaizen_root = root / "kaizen"
    if not kaizen_root.is_dir():
        return errors
    required_headings = (
        "Gözlem",
        "Kök neden hipotezi",
        "En küçük değişiklik",
        "Doğrulama",
        "Rollback",
    )
    required_fields = (
        ("Baseline/hash", r"^- Baseline/hash:\s*\S.+$"),
        ("10. Adam karşı görüşü", r"^- 10\. Adam karşı görüşü:\s*\S.+$"),
        (
            "Yanlış genelleme sınırı",
            r"^- Yanlış genelleme sınırı:\s*\S.+$",
        ),
    )
    name_pattern = re.compile(r"^KZ-\d{8}-\d{3}-[a-z0-9-]+\.md$")
    for path in sorted(kaizen_root.glob("KZ-*.md")):
        if not name_pattern.fullmatch(path.name):
            errors.append(f"Geçersiz Kaizen dosya adı: {path.name}")
        try:
            text = _read_utf8(path)
        except UnicodeDecodeError:
            continue
        headings = {
            match.group(1).strip()
            for match in re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE)
        }
        for heading in required_headings:
            if heading not in headings:
                errors.append(f"Eksik Kaizen başlığı: {path.name} -> {heading}")
        if not any(heading.startswith("Sonuç") for heading in headings):
            errors.append(f"Eksik Kaizen başlığı: {path.name} -> Sonuç")
        for label, pattern in required_fields:
            if not re.search(pattern, text, re.MULTILINE):
                errors.append(f"Eksik Kaizen alanı: {path.name} -> {label}")
    return errors


def check_nonempty_markdown(root: Path) -> list[str]:
    errors: list[str] = []
    for path in _markdown_files(root):
        try:
            text = _read_utf8(path)
        except UnicodeDecodeError:
            continue
        meaningful = [line for line in text.splitlines() if line.strip()]
        if len(meaningful) < 2 or not meaningful[0].startswith("#"):
            errors.append(f"Eksik veya başlıksız Markdown: {_display(path, root)}")
    return errors


def run_checks(root: Path = REPO_ROOT) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    checks = (
        check_structure,
        check_text_format,
        check_markdown_links,
        check_index_reachability,
        check_role_cards,
        check_prompt_contract,
        check_bootstrap_contract,
        check_kaizen_records,
        check_nonempty_markdown,
    )
    for check in checks:
        errors.extend(check(root))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Denetlenecek repo kökü",
    )
    parser.add_argument(
        "--policy-epoch",
        action="store_true",
        help="AGENTS, SYSTEM_PROMPT ve INDEX için birleşik SHA-256 yazdır",
    )
    parser.add_argument(
        "--verify-bootstrap",
        action="store_true",
        help="Control plane tracked, committed ve HEAD ile aynı mı denetle",
    )
    args = parser.parse_args(argv)
    if args.policy_epoch:
        try:
            print(f"POLICY_EPOCH={calculate_policy_epoch(args.root.resolve())}")
            print(f"POLICY_EPOCH_ALGORITHM={POLICY_EPOCH_ALGORITHM}")
        except OSError as exc:
            print(f"POLICY_EPOCH_FAIL: {exc}")
            return 1
        return 0
    if args.verify_bootstrap:
        errors = check_git_control_plane(args.root)
        if errors:
            print(f"BOOTSTRAP_VERIFY_FAIL ({len(errors)} bulgu)")
            for error in errors:
                print(f"- {error}")
            return 1
        print(f"BOOTSTRAP_VERIFY_OK POLICY_EPOCH={calculate_policy_epoch(args.root)}")
        return 0
    errors = run_checks(args.root)
    if errors:
        print(f"REPO_CHECK_FAIL ({len(errors)} bulgu)")
        for error in errors:
            print(f"- {error}")
        return 1
    print("REPO_CHECK_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
