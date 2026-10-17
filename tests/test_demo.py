"""Test the public first-run experience with installed service/file adapters."""
import unittest
from tools.demo import run_demo


class PublicDemoTests(unittest.TestCase):
    def test_demo_proves_roundtrip_and_two_file_evidence_records(self):
        result = run_demo()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["evidence_records"], 2)
        self.assertEqual(result["evidence_kind"], "FILE_TRANSFORM")
        self.assertFalse(result["hardware_contacted"])
        self.assertFalse(result["vendor_compile"])
