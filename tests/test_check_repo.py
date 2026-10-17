"""Repo sözleşmesi denetleyicisinin birim testleri."""

from __future__ import annotations

import sys
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import check_repo


class RepositoryContractTests(unittest.TestCase):
    def test_repository_passes_all_checks(self):
        self.assertEqual([], check_repo.run_checks(REPO_ROOT))


class CheckerFailureTests(unittest.TestCase):
    def test_missing_required_file_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            errors = check_repo.check_structure(Path(temporary))
        self.assertIn("Eksik dosya: README.md", errors)

    def test_broken_markdown_link_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "INDEX.md").write_text(
                "# İndeks\n\n[Kayıp](missing.md)\n", encoding="utf-8"
            )
            errors = check_repo.check_markdown_links(root)
        self.assertTrue(any("Kırık bağlantı" in error for error in errors))

    def test_broken_anchor_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "INDEX.md").write_text(
                "# İndeks\n\n[Yanlış](guide.md#olmayan)\n", encoding="utf-8"
            )
            (root / "guide.md").write_text(
                "# Rehber\n\n## Var olan\n", encoding="utf-8"
            )
            errors = check_repo.check_markdown_links(root)
        self.assertTrue(any("Kırık anchor" in error for error in errors))

    def test_unreachable_markdown_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "INDEX.md").write_text("# İndeks\n\nBaşlangıç.\n", encoding="utf-8")
            (root / "orphan.md").write_text("# Yetim\n\nİçerik.\n", encoding="utf-8")
            errors = check_repo.check_index_reachability(root)
        self.assertIn("INDEX.md üzerinden erişilemiyor: orphan.md", errors)

    def test_missing_role_heading_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            role = root / "prompts/roles/ANALYST.md"
            role.parent.mkdir(parents=True)
            role.write_text(
                "# ANALYST\n\n"
                "[SYSTEM_PROMPT.md](../../SYSTEM_PROMPT.md)\n\n"
                "## Okunacaklar\n\n- Kanıt\n",
                encoding="utf-8",
            )
            errors = check_repo.check_role_cards(root)
        self.assertIn(
            "Eksik rol başlığı: prompts/roles/ANALYST.md -> Amaç",
            errors,
        )

    def test_crlf_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "note.md"
            path.write_bytes(b"# Note\r\n\r\nText\r\n")
            errors = check_repo.check_text_format(root)
        self.assertIn("LF dışında satır sonu var: note.md", errors)

    def test_git_repo_check_ignores_tracked_vendor_tree_outside_contract_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(("git", "init", "-q", str(root)), check=True)
            subprocess.run(
                ("git", "-C", str(root), "config", "core.autocrlf", "false"),
                check=True,
            )
            (root / "README.md").write_bytes(b"# Repo\n\nText.\n")
            vendor = root / "vendor"
            vendor.mkdir()
            (vendor / "bad.py").write_bytes(b"\xef\xbb\xbfvalue = 1\r\n")
            subprocess.run(
                ("git", "-C", str(root), "add", "README.md", "vendor/bad.py"),
                check=True,
            )
            errors = check_repo.check_text_format(root)
        self.assertEqual([], errors)

    def test_missing_bootstrap_token_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "AGENTS.md").write_text(
                "# Agents\n\nBOOTSTRAP_ACK\nPOLICY_EPOCH\n", encoding="utf-8"
            )
            (root / "SYSTEM_PROMPT.md").write_text(
                "# Prompt\n\nBOOTSTRAP_ACK\nPOLICY_EPOCH\n"
                "RESUME_ACK\nSESSION_CHECKPOINT.md\n",
                encoding="utf-8",
            )
            errors = check_repo.check_bootstrap_contract(root)
        self.assertIn(
            "Bootstrap sözleşmesi eksik: AGENTS.md -> RESUME_ACK",
            errors,
        )

    def test_policy_epoch_changes_with_control_plane(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in check_repo.CONTROL_PLANE_FILES:
                (root / relative).write_text(f"# {relative}\n", encoding="utf-8")
            before = check_repo.calculate_policy_epoch(root)
            (root / "INDEX.md").write_text("# INDEX\n\nDeğişti.\n", encoding="utf-8")
            after = check_repo.calculate_policy_epoch(root)
        self.assertNotEqual(before, after)

    def test_incomplete_kaizen_record_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kaizen = root / "kaizen"
            kaizen.mkdir()
            (kaizen / "KZ-20260824-001-test.md").write_text(
                "# KZ-20260824-001 — Test\n\n- Baseline/hash: abc\n",
                encoding="utf-8",
            )
            errors = check_repo.check_kaizen_records(root)
        self.assertTrue(any("Eksik Kaizen alanı" in error for error in errors))

    def test_kaizen_token_soup_does_not_bypass_structure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kaizen = root / "kaizen"
            kaizen.mkdir()
            (kaizen / "KZ-20260824-001-token-soup.md").write_text(
                "# KZ Token Soup\n\n"
                "Baseline/hash Gözlem Kök neden hipotezi 10. Adam karşı görüşü "
                "En küçük değişiklik Doğrulama Rollback Sonuç "
                "Yanlış genelleme sınırı\n",
                encoding="utf-8",
            )
            errors = check_repo.check_kaizen_records(root)
        self.assertTrue(any("Eksik Kaizen başlığı" in error for error in errors))

    def test_valid_minimal_kaizen_record_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kaizen = root / "kaizen"
            kaizen.mkdir()
            (kaizen / "KZ-20260824-001-valid.md").write_text(
                "# KZ-20260824-001 — Valid\n\n"
                "- Baseline/hash: `abc`\n\n"
                "## Gözlem\n\nMetin.\n\n"
                "## Kök neden hipotezi\n\n"
                "- 10. Adam karşı görüşü: Karşı örnek.\n\n"
                "## En küçük değişiklik\n\nMetin.\n\n"
                "## Doğrulama\n\nMetin.\n\n"
                "## Rollback\n\nMetin.\n\n"
                "## Sonuç ve sınır\n\n"
                "- Yanlış genelleme sınırı: Bağlama bağlı.\n",
                encoding="utf-8",
            )
            errors = check_repo.check_kaizen_records(root)
        self.assertEqual([], errors)

    def test_invalid_kaizen_filename_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kaizen = root / "kaizen"
            kaizen.mkdir()
            (kaizen / "KZ-wrong.md").write_text(
                "# KZ Wrong\n\n- Baseline/hash: `abc`\n",
                encoding="utf-8",
            )
            errors = check_repo.check_kaizen_records(root)
        self.assertIn("Geçersiz Kaizen dosya adı: KZ-wrong.md", errors)

    def test_git_control_plane_detects_worktree_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ("git", "init", "-q", str(root)), check=True, capture_output=True
            )
            subprocess.run(
                ("git", "-C", str(root), "config", "user.name", "Repo Test"),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ("git", "-C", str(root), "config", "user.email", "repo@test.invalid"),
                check=True,
                capture_output=True,
            )
            for relative in check_repo.CONTROL_PLANE_FILES:
                (root / relative).write_bytes(f"# {relative}\n".encode("utf-8"))
            subprocess.run(
                ("git", "-C", str(root), "add", *check_repo.CONTROL_PLANE_FILES),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ("git", "-C", str(root), "commit", "-q", "-m", "test"),
                check=True,
                capture_output=True,
            )
            self.assertEqual([], check_repo.check_git_control_plane(root))
            (root / "AGENTS.md").write_bytes(b"# Drift\n")
            errors = check_repo.check_git_control_plane(root)
        self.assertTrue(any("HEAD ile aynı değil" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
