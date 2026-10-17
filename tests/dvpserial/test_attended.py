"""Independent wire-level regression for the imported attended M/D gate.

All ports and live proofs are synthetic. No hardware is contacted.
"""
from datetime import datetime, timedelta, timezone
import tempfile
import unittest

from test_controls import Scenario, save


class AttendedTests(unittest.TestCase):
    def scenario(self, root):
        s = Scenario(root)
        s.install_test_parameters()
        s.config.update(heaters_isolated=False, attended_machine_test_authorized=True,
                        independent_power_cutoff_available=True)
        return s

    def test_attended_m_and_d_use_existing_wire_paths(self):
        with tempfile.TemporaryDirectory() as root:
            s = self.scenario(root)
            self.assertTrue(s.command(channel=1)['ok'])
            result = s.parameter()
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['readback_verified'])
            self.assertEqual(s.state.parameter_mutations, [(2000, 2000)])

    def test_missing_false_and_truthy_values_cannot_authorize(self):
        for field in ('attended_machine_test_authorized', 'independent_power_cutoff_available',
                      'user_present', 'machine_control_enabled'):
            for value in (None, False, 1, 'true'):
                for op in ('m', 'd'):
                    with self.subTest(field=field, value=value, op=op), tempfile.TemporaryDirectory() as root:
                        s = self.scenario(root)
                        if value is None:
                            s.config.pop(field)
                        else:
                            s.config[field] = value
                        result = s.command(channel=1) if op == 'm' else s.parameter()
                        self.assertFalse(result['ok'], result)
                        self.assertEqual(s.state.mutations, [])
                        self.assertEqual(s.state.parameter_mutations, [])

    def test_isolated_legacy_profiles_still_work(self):
        with tempfile.TemporaryDirectory() as root:
            s = Scenario(root)
            s.install_test_parameters()
            self.assertTrue(s.command(channel=1)['ok'])
            self.assertTrue(s.parameter()['ok'])

    def test_attended_does_not_grant_run_stop_or_transfer(self):
        for operation in ('target.run', 'target.stop', 'project.upload', 'deployment.execute'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as root:
                s = self.scenario(root)
                s.config.update(allow_run_stop=True, program_transfer_enabled=True,
                                allow_program_transfer=True, verified_contract=True)
                result = s.call(operation)
                self.assertFalse(result['ok'], result)
                self.assertEqual(s.state.mutations, [])
                self.assertEqual(s.state.parameter_mutations, [])

    def test_attended_keeps_source_and_lease_guards(self):
        for mode in ('source', 'lease', 'expired'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                s = self.scenario(root)
                self.assertTrue(s.call('machine.read', device='D100')['ok'])
                owned = s.state.connections[-1]
                if mode == 'source':
                    s.source.write_bytes(b'CHANGED')
                elif mode == 'lease':
                    s.lease_data['owner'] = 'another-agent'
                    save(s.lease, s.lease_data)
                else:
                    s.config['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
                self.assertFalse(s.parameter()['ok'])
                self.assertEqual(s.state.parameter_mutations, [])
                self.assertTrue(owned.closed)

    def test_attended_revoke_at_write_boundary_sends_no_fc06(self):
        with tempfile.TemporaryDirectory() as root:
            s = self.scenario(root)
            def revoke(state):
                from pathlib import Path
                journals = list(Path(s.config['machine_state_dir']).glob('command-*.json'))
                if any('write_intent' in p.read_text('utf-8') for p in journals):
                    s.config['independent_power_cutoff_available'] = False
                    state.on_reset = None
            s.state.on_reset = revoke
            self.assertFalse(s.parameter()['ok'])
            self.assertEqual(s.state.parameter_mutations, [])


if __name__ == '__main__':
    unittest.main()
