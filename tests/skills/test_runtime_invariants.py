"""S0 package/skill work must not replace the established execution engines."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from .test_skill_package import ROOT,package


class RuntimeInvariants(unittest.TestCase):
    def test_existing_executable_source_matches_the_reviewed_baseline(self):
        value=json.loads((ROOT/'docs/plans/plc-skill/RUNTIME_BASELINE.json').read_text())
        for name,wanted in value['files'].items():
            self.assertEqual(hashlib.sha256((ROOT/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest(),wanted,name)

    def test_loaded_source_cannot_hide_an_older_installed_distribution(self):
        manifest=json.loads((package.SKILL/'references/compatibility.json').read_text())
        with patch.object(package.importlib.metadata,'version',return_value='0.3.1'):
            with self.assertRaises(ValueError):package.installed_inventory(manifest)


if __name__=='__main__':unittest.main()
