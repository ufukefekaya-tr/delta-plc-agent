"""Executable task-envelope and compaction-resume guard tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import check_repo, session_guard


class SessionGuardTests(unittest.TestCase):
    def setUp(self):
        self.repo_temp = tempfile.TemporaryDirectory()
        self.state_temp = tempfile.TemporaryDirectory()
        self.root = Path(self.repo_temp.name)
        self.state_root = Path(self.state_temp.name)
        subprocess.run(("git", "init", "-q", str(self.root)), check=True)
        subprocess.run(
            ("git", "-C", str(self.root), "config", "user.name", "Guard Test"),
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(self.root), "config", "core.autocrlf", "false"),
            check=True,
        )
        subprocess.run(
            (
                "git",
                "-C",
                str(self.root),
                "config",
                "user.email",
                "guard@test.invalid",
            ),
            check=True,
        )
        for relative in check_repo.CONTROL_PLANE_FILES:
            (self.root / relative).write_bytes(f"# {relative}\n".encode("utf-8"))
        subprocess.run(
            ("git", "-C", str(self.root), "add", *check_repo.CONTROL_PLANE_FILES),
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(self.root), "commit", "-q", "-m", "baseline"),
            check=True,
        )
        self.envelope_path = self.state_root / "TASK_ENVELOPE.json"
        self.checkpoint_path = self.state_root / "SESSION_CHECKPOINT.json"
        self.envelope = {
            "schema_version": 2,
            "task_id": "T-001",
            "baseline_commit": session_guard.current_head(self.root),
            "policy_epoch_algorithm": check_repo.POLICY_EPOCH_ALGORITHM,
            "policy_epoch": check_repo.calculate_policy_epoch(self.root),
            "mode": "NORMAL",
            "role": "PROGRAMMER",
            "owner": "orchestrator",
            "authority_source": "unit test",
            "allowed_read_paths": ["docs/"],
            "allowed_write_paths": ["tools/"],
            "protected_paths": ["PLC/"],
            "preexisting_dirty_paths": [],
            "commit_policy": "SCOPED_MANDATORY",
            "push_authorized": False,
            "acceptance_checks": ["python -m unittest"],
        }
        self._write_state()

    def tearDown(self):
        self.state_temp.cleanup()
        self.repo_temp.cleanup()

    def _write_state(self):
        self.envelope_path.write_text(
            json.dumps(self.envelope, indent=2) + "\n", encoding="utf-8"
        )
        self.checkpoint = {
            "schema_version": 2,
            "task_id": self.envelope["task_id"],
            "sequence": 1,
            "envelope_sha256": session_guard.file_sha256(self.envelope_path),
            "policy_epoch_algorithm": check_repo.POLICY_EPOCH_ALGORITHM,
            "policy_epoch": check_repo.calculate_policy_epoch(self.root),
            "repo_head": session_guard.current_head(self.root),
            "worktree_status_sha256": session_guard.worktree_status_sha256(self.root),
            "completed_gate": "bootstrap",
            "evidence": ["control plane clean"],
            "open_items": [],
            "assumptions": [],
            "next_step": "Verify one allowed edit",
        }
        self.checkpoint_path.write_text(
            json.dumps(self.checkpoint, indent=2) + "\n", encoding="utf-8"
        )

    def _resume_errors(self):
        return session_guard.validate_checkpoint(
            self.root,
            self.envelope_path,
            session_guard.load_json_object(self.envelope_path),
            session_guard.load_json_object(self.checkpoint_path),
        )

    def test_policy_epoch_algorithm_is_exact_and_stable(self):
        digest = hashlib.sha256()
        for relative in check_repo.CONTROL_PLANE_FILES:
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update((self.root / relative).read_bytes())
            digest.update(b"\0")
        self.assertEqual(digest.hexdigest(), check_repo.calculate_policy_epoch(self.root))

    def test_valid_checkpoint_resumes(self):
        self.assertEqual([], self._resume_errors())

    def test_stale_policy_epoch_is_rejected(self):
        self.envelope["policy_epoch"] = "0" * 64
        self._write_state()
        self.assertTrue(any("policy epoch" in error for error in self._resume_errors()))

    def test_worktree_drift_after_checkpoint_is_rejected(self):
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "change.py").write_text("value = 1\n", encoding="utf-8")
        self.assertTrue(any("worktree özeti" in error for error in self._resume_errors()))

    def test_allowed_edit_path_passes(self):
        errors = session_guard.verify_action(
            self.root,
            self.envelope_path,
            self.checkpoint_path,
            "edit",
            "tools/change.py",
        )
        self.assertEqual([], errors)

    def test_out_of_scope_edit_is_rejected(self):
        errors = session_guard.verify_action(
            self.root,
            self.envelope_path,
            self.checkpoint_path,
            "edit",
            "projects/machine/program.st",
        )
        self.assertTrue(any("scope dışında" in error for error in errors))

    def test_protected_path_wins_over_write_allowlist(self):
        self.envelope["allowed_write_paths"] = ["PLC/"]
        self._write_state()
        errors = session_guard.verify_action(
            self.root,
            self.envelope_path,
            self.checkpoint_path,
            "edit",
            "PLC/program.st",
        )
        self.assertTrue(any("Korunan" in error for error in errors))

    def test_protected_path_can_still_be_read_when_explicitly_allowed(self):
        self.envelope["allowed_read_paths"] = ["PLC/"]
        self._write_state()
        errors = session_guard.verify_action(
            self.root,
            self.envelope_path,
            self.checkpoint_path,
            "read",
            "PLC/program.st",
        )
        self.assertEqual([], errors)

    def test_commit_needs_staged_scope_but_no_permission_flag(self):
        errors = session_guard.verify_action(
            self.root,
            self.envelope_path,
            self.checkpoint_path,
            "commit",
        )
        self.assertIn("Commit penceresinde index boş", errors)
        self.assertFalse(any("yetkisi" in error.lower() for error in errors))

    def test_mandatory_commit_accepts_only_staged_write_scope(self):
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "change.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(
            ("git", "-C", str(self.root), "add", "tools/change.py"), check=True
        )
        self._write_state()
        errors = session_guard.verify_action(
            self.root, self.envelope_path, self.checkpoint_path, "commit"
        )
        self.assertEqual([], errors)

    def test_preexisting_dirty_path_cannot_enter_stage_outside_write_scope(self):
        notes = self.root / "notes"
        notes.mkdir()
        (notes / "user.txt").write_text("user work\n", encoding="utf-8")
        subprocess.run(
            ("git", "-C", str(self.root), "add", "notes/user.txt"), check=True
        )
        self.envelope["preexisting_dirty_paths"] = ["notes/user.txt"]
        self._write_state()
        errors = session_guard.verify_action(
            self.root, self.envelope_path, self.checkpoint_path, "commit"
        )
        self.assertIn("Stage scope dışında: notes/user.txt", errors)


if __name__ == "__main__":
    unittest.main()
