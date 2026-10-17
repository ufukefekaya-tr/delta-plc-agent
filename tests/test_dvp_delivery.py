"""Installer selection and honest prerequisite classification without hardware."""
import importlib.util
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'tools'/f'{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class DeliveryTests(unittest.TestCase):
    def test_dvp_installer_never_installs_ax_and_uses_absolute_source(self):
        installer = module('install')
        with patch.object(sys, 'argv', ['install', '--profile', 'dvp']), patch.object(installer.subprocess, 'run') as run:
            installer.main()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], str(ROOT))
        self.assertNotIn('ax', run.call_args.args[0])

    def test_default_installer_retains_all_backend_behavior(self):
        installer = module('install')
        with patch.object(sys, 'argv', ['install']), patch.object(installer.subprocess, 'run') as run:
            installer.main()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][-1], str(ROOT/'backends/ax'))

    def test_package_failure_stops_installer(self):
        installer = module('install')
        with patch.object(sys, 'argv', ['install', '--profile', 'dvp']), patch.object(installer.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'pip')):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.main()

    def test_inventory_does_not_confuse_missing_hardware_with_missing_driver(self):
        result = module('dvp_prerequisites').inspect('serial_ascii', 'COM5', True, {'tools': {}}, [])
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('WPLSOFT_MISSING', result['issues'])
        self.assertFalse(result['plc_contacted'])
        self.assertTrue(any('hardware disconnected or driver unavailable' in s for s in result['issues']))

    def test_cdc_mismatch_blocks_without_opening_any_port(self):
        result = module('dvp_prerequisites').inspect('directlink_cdc', 'COM6', False, {},
                   [{'port': 'COM6', 'vid': 0x1A86, 'pid': 0x7523}])
        self.assertIn('CDC_GATEWAY_MISMATCH', result['issues'])
        self.assertFalse(result['port_opened'])

    def test_found_gateway_is_not_plc_acceptance(self):
        result = module('dvp_prerequisites').inspect('directlink_cdc', 'COM6', True,
                   {'tools': {'wplsoft': [{'sha256': 'test'}]}},
                   [{'port': 'COM6', 'vid': 0x05DD, 'pid': 0x0100}])
        self.assertEqual(result['status'], 'PREREQUISITES_FOUND')
        self.assertFalse(result['live_verified'])

    def test_workflow_separates_vendor_ui_from_typed_mcp(self):
        workflow = json.loads((ROOT/'workflows/dvp-final.json').read_bytes())
        known = json.loads((ROOT/'src/universal_plc/data/workflows.json').read_bytes())
        for stage in workflow['stages']:
            self.assertIn(stage['workflow'], known)
            if stage['executor'] == 'mcp':
                self.assertTrue(set(stage['operations']) <= set(known[stage['workflow']]['operations']))
        watch = next(s for s in workflow['stages'] if s['id'] == 'watch')
        self.assertEqual(watch['executor'], 'mcp')
        self.assertNotIn('deployment.execute', watch['operations'])
        self.assertEqual(next(s for s in workflow['stages'] if s['id'] == 'download')['executor'], 'computer_use')

    def test_wheel_and_package_versions_agree(self):
        import tomllib
        from universal_plc import __version__
        self.assertEqual(tomllib.loads((ROOT/'pyproject.toml').read_text('utf-8'))['project']['version'], __version__)


class ObserveDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.helper = module('dvp_controls_observe')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        source, catalog, config = [root / name for name in ('source.dvp', 'catalog.json', 'config.json')]
        source.write_bytes(b'synthetic-source')
        catalog.write_text(json.dumps({'revision': 'TEST'}), encoding='utf-8')
        config.write_text(json.dumps(dict(target_model='DVP14SS211R',
            machine_source_path=str(source), machine_source_sha256=self.helper.sha(source),
            machine_catalog_path=str(catalog), machine_catalog_sha256=self.helper.sha(catalog))), encoding='utf-8')
        self.args = SimpleNamespace(config=str(config), runtime_dir=str(root/'runtime'), workspace_id='ws_enrolled',
                                    observe=True, duration_seconds=1, interval_seconds=1)
        self.ws = dict(workspace_id='ws_enrolled', adapter_id='delta.dvpserial',
                       target=dict(vendor='delta', family='DVP', model='DVP14SS211R', toolchain='dvpserial'),
                       revision='TEST', baseline_sha256=self.helper.sha(source), context_digest='pinned-context')
        self.calls, self.report = [], {}
        self.live, self.mutate = True, False

    def execute(self):
        owner = self

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def call_tool(self, name, args):
                owner.calls.append((name, args))
                if name in ('workspace.status', 'workspace.create'):
                    value = owner.ws
                elif name == 'workflow.start':
                    value = {'run_id': 'run_test'}
                elif name == 'workflow.run':
                    op = args['steps'][0]['operation']
                    value = {'completed': [{'result': {'ok': True, 'live_revision_verified': owner.live}}]}
                    if owner.mutate and op == 'machine.profile':
                        Path(owner.args.config).write_text('{}', encoding='utf-8')
                elif name == 'adapter.invoke':
                    assert args['operation'] == 'target.disconnect'
                    value = {'result': {'ok': True}}
                else:
                    raise AssertionError(name)
                return SimpleNamespace(is_error=False, structured_content=value)

        ticks = iter([0, 2])
        with patch.object(self.helper, 'Client', FakeClient), patch.object(self.helper, 'time', SimpleNamespace(monotonic=lambda: next(ticks))):
            asyncio.run(self.helper.run(self.args, self.report, lambda: None))

    def test_verified_watch_reuses_enrolled_context_and_closes(self):
        self.execute()
        self.assertEqual(self.calls[0], ('workspace.status', {'workspace_id': 'ws_enrolled'}))
        self.assertNotIn('workspace.create', [name for name, _ in self.calls])
        self.assertEqual(self.report['outcome'], 'OBSERVATIONS_RECORDED')
        self.assertEqual(self.calls[-1][1]['expected_context_digest'], 'pinned-context')

    def test_mismatched_workspace_is_rejected_before_any_operation(self):
        for key, value in [('revision', 'OLD'), ('baseline_sha256', 'wrong'), ('context_digest', ''),
                           ('workspace_id', 'ws_other'), ('target', {'model': 'OTHER'}), ('adapter_id', 'delta.ax')]:
            with self.subTest(key=key):
                original = self.ws[key]
                self.ws[key] = value
                self.calls.clear()
                with self.assertRaisesRegex(ValueError, 'mismatch'):
                    self.execute()
                self.assertEqual([name for name, _ in self.calls], ['workspace.status'])
                self.ws[key] = original

    def test_unverified_sample_is_retained_but_never_passed_or_retried(self):
        self.live = False
        with self.assertRaisesRegex(RuntimeError, 'LIVE_REVISION_UNVERIFIED'):
            self.execute()
        self.assertFalse(self.report['observations'][0]['live_revision_verified'])
        self.assertNotEqual(self.report.get('outcome'), 'OBSERVATIONS_RECORDED')
        self.assertEqual(self.calls[-1][0], 'adapter.invoke')
        self.assertEqual(sum(name == 'workflow.run' for name, _ in self.calls), 2)

    def test_watch_without_workspace_fails_before_client(self):
        self.args.workspace_id = None
        with self.assertRaisesRegex(ValueError, '--workspace-id'):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_profile_only_keeps_offline_new_workspace_behavior(self):
        self.args.workspace_id = None
        self.args.observe = False
        self.execute()
        self.assertEqual(self.calls[0][0], 'workspace.create')
        self.assertEqual(self.report['outcome'], 'PASS_PROFILE_ONLY_NO_HARDWARE')

    def test_profile_drift_stops_watch_and_disconnects(self):
        self.mutate = True
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            self.execute()
        self.assertEqual(sum(name == 'workflow.run' for name, _ in self.calls), 1)
        self.assertEqual(self.calls[-1][0], 'adapter.invoke')


if __name__ == '__main__':
    unittest.main()
