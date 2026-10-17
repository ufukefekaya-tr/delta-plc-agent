from __future__ import annotations

import subprocess
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from . import PACKAGE_ROOT


class ReleasePackagingTests(unittest.TestCase):
    def test_wheel_contains_every_runtime_worker_and_codex_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            built = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--no-isolation",
                    "--wheel",
                    "--outdir",
                    str(output),
                    str(PACKAGE_ROOT),
                ],
                cwd=PACKAGE_ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            wheels = tuple(output.glob("universal_plc_axmcp-0.2.0-*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as archive:
                names = set(archive.namelist())
            required = {
                "axmcp_offline/ax_worker/worker.py",
                "axmcp_offline/ax_worker/ladder_import.py",
                "axmcp_offline/ax_worker/project_factory.py",
                "axmcp_offline/ax_deployment_worker/worker.py",
                "axmcp_offline/ax_deployment_worker/ax308_sp18_target.py",
                "axmcp_offline/contracts/v2/deployment_reconciliation.schema.json",
                "axmcp_offline/templates/codex-config.example.toml",
            }
            self.assertEqual(required - names, set())
            installed = output / "installed"
            install = subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", "--target", str(installed), str(wheels[0])], capture_output=True, text=True, timeout=60)
            self.assertEqual(install.returncode, 0, install.stderr)
            foreign = output / "unrelated-cwd"
            foreign.mkdir()
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(installed)
            environment["AXMCP_MODE"] = "recorded_replay"
            environment.pop("AXMCP_REPLAY_BUNDLE", None)
            code = '''
from pathlib import Path
from importlib.resources import files
from axmcp_offline.cli import _packaged_ax_worker, _packaged_ax_deployment_worker
from axmcp_offline.universal_adapter import AxAdapter
root = Path(str(files('axmcp_offline')))
for item in (*_packaged_ax_worker(), _packaged_ax_deployment_worker()):
    assert item.is_relative_to(root), item
assert root.joinpath('ax_worker/project_factory.py').is_file()
assert root.joinpath('contracts/v2/ladder_recipe.schema.json').is_file()
result = AxAdapter().invoke('environment.probe', {})
assert result['evidence_class'] == 'SIMULATED'
assert result['live_ax_verified'] is False
print('INSTALLED_AX_WHEEL_PORTABLE_OK')
'''
            checked = subprocess.run([sys.executable, "-c", code], cwd=foreign, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("INSTALLED_AX_WHEEL_PORTABLE_OK", checked.stdout)


if __name__ == "__main__":
    unittest.main()
