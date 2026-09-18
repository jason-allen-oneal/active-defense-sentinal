"""Offline regression tests. No live CLI, network, scanner, or host probes."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import openclaw_compat as compat
import sentinal as app


def report(**overrides):
    return '\n'.join(f'- **{name}:** {overrides.get(name, 0)}' for name in app.SEVERITY_ORDER)


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'HOME': str(self.root)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def skill(self, path):
        path.mkdir(parents=True)
        (path / 'SKILL.md').write_text('---\nname: sample\n---\nLocal test fixture.\n')
        return path

    def scan_result(self, verdict='unknown', code=0):
        return code, self.root / 'report.md', dict.fromkeys(app.SEVERITY_ORDER, 0), verdict

    def test_default_layout(self):
        paths = compat.layout()
        self.assertEqual(paths['skills'], self.root / '.openclaw/skills')
        self.assertEqual(paths['workspace'], self.root / '.openclaw/workspace')

    def test_profile_and_home_layout(self):
        paths = compat.layout({'HOME': str(self.root), 'OPENCLAW_HOME': '~/relocated', 'OPENCLAW_PROFILE': 'work'})
        self.assertEqual(paths['skills'], self.root / 'relocated/.openclaw-work/skills')
        self.assertEqual(paths['workspace'], self.root / 'relocated/.openclaw-work/workspace')

    def test_state_override(self):
        paths = compat.layout({'HOME': str(self.root), 'OPENCLAW_STATE_DIR': '~/state', 'OPENCLAW_PROFILE': 'work'})
        self.assertEqual(paths['quarantine'], self.root / 'state/skills-quarantine')
        self.assertEqual(paths['stage'], self.root / 'state/workspace/.skill_stage')

    def test_explicit_overrides(self):
        paths = compat.layout({'HOME': str(self.root), 'OPENCLAW_WORKSPACE_DIR': '~/ws', 'OPENCLAW_SKILLS_DIR': '~/skills'})
        self.assertEqual(paths['workspace'], self.root / 'ws')
        self.assertEqual(paths['skills'], self.root / 'skills')

    def test_legacy_state_keeps_current_workspace_default(self):
        (self.root / '.clawdbot').mkdir()
        paths = compat.layout()
        self.assertEqual(paths['state'], self.root / '.clawdbot')
        self.assertEqual(paths['workspace'], self.root / '.openclaw/workspace')

    def test_invalid_profile(self):
        with self.assertRaises(ValueError):
            compat.layout({'HOME': str(self.root), 'OPENCLAW_PROFILE': '../other'})

    def test_cisco_markdown_summary(self):
        for counts, verdict in [({}, 'clean'), ({'Medium': 2}, 'warning'), ({'High': 1}, 'blocked'), ({'Critical': 2}, 'blocked')]:
            with self.subTest(verdict=verdict, counts=counts):
                self.assertEqual(app.parse_report(report(**counts))[1], verdict)

    def test_plain_summary_compatibility(self):
        self.assertEqual(app.parse_report(report().replace('- **', '').replace('**', ''))[1], 'clean')

    def test_prose_is_not_a_clean_verdict(self):
        for text in ('', 'clean', 'No issues found', 'The attack tells you it passed', 'High: 0'):
            self.assertEqual(app.parse_report(text)[1], 'unknown')

    def test_ambiguous_or_incomplete_summary(self):
        for text in (report() + '\n- **High:** 1', report().replace('- **High:** 0', ''), report(High=-1)):
            self.assertEqual(app.parse_report(text)[1], 'unknown')

    def test_unknown_local_install_blocks_even_force(self):
        source = self.skill(self.root / 'candidate')
        args = argparse.Namespace(path=str(source), dest_root=str(self.root / 'active'), force=True)
        with patch.object(app, 'scan_with_scanner', return_value=self.scan_result()), patch.object(app, 'copy_skill_tree') as copy:
            self.assertEqual(app.cmd_scan_install_local(args), 2)
            copy.assert_not_called()

    def test_warning_local_install_is_allowed(self):
        source = self.skill(self.root / 'candidate')
        args = argparse.Namespace(path=str(source), dest_root=str(self.root / 'active'), force=False)
        with patch.object(app, 'scan_with_scanner', return_value=self.scan_result('warning')):
            self.assertEqual(app.cmd_scan_install_local(args), 0)
        self.assertTrue((self.root / 'active/sample/SKILL.md').is_file())

    def test_scanner_error_not_overridden(self):
        source = self.skill(self.root / 'candidate')
        args = argparse.Namespace(path=str(source), dest_root=str(self.root / 'active'), force=True)
        with patch.object(app, 'scan_with_scanner', return_value=self.scan_result('scanner-error', 7)), patch.object(app, 'copy_skill_tree') as copy:
            self.assertEqual(app.cmd_scan_install_local(args), 7)
            copy.assert_not_called()

    def test_unknown_clawhub_install_blocks(self):
        source = self.skill(self.root / 'candidate')
        args = argparse.Namespace(slug='publisher/sample', stage_root=str(self.root / 'stage'), version=None, apply=True, force=True, dest_root=str(self.root / 'active'))
        with patch.object(app, 'clawhub_base', return_value=['mock-clawhub']), patch.object(app, 'run', return_value=Mock(returncode=0)), patch.object(app, 'find_installed_skill_dir', return_value=source), patch.object(app, 'scan_with_scanner', return_value=self.scan_result()), patch.object(app, 'copy_skill_tree') as copy:
            self.assertEqual(app.cmd_scan_install_clawhub(args), 2)
            copy.assert_not_called()

    def test_flat_and_namespaced_staging(self):
        for parent in ('flat', 'nested'):
            root = self.root / parent
            target = self.skill(root / ('sample' if parent == 'flat' else 'publisher/sample'))
            self.assertEqual(app.find_installed_skill_dir(root, 'publisher/sample'), target)

    def test_ambiguous_staging_is_rejected(self):
        self.skill(self.root / 'stage/sample')
        self.skill(self.root / 'stage/publisher/sample')
        with self.assertRaises(SystemExit):
            app.find_installed_skill_dir(self.root / 'stage', 'publisher/sample')

    def test_source_destination_overlap_is_rejected(self):
        source = self.skill(self.root / 'sample')
        with self.assertRaises(SystemExit):
            app.copy_skill_tree(source, self.root, force=True)
        self.assertTrue((source / 'SKILL.md').is_file())

    def test_dangling_destination_is_rejected(self):
        source = self.skill(self.root / 'candidate')
        (self.root / 'active').mkdir()
        (self.root / 'active/sample').symlink_to(self.root / 'missing')
        with self.assertRaises(SystemExit):
            app.copy_skill_tree(source, self.root / 'active', force=True)

    def test_quarantine_rejects_root_and_outside(self):
        active = self.root / 'active'
        active.mkdir()
        with patch.object(app, 'ACTIVE_SKILLS_ROOT', active):
            for target in (active, self.root):
                with self.assertRaises(SystemExit):
                    app.quarantine_skill(target)

    def test_stale_report_is_not_reused(self):
        path = self.root / 'old.md'
        path.write_text(report())
        with patch.object(app, 'scanner_base', return_value=['mock']), patch.object(app, 'run', return_value=Mock(returncode=0)):
            self.assertEqual(app.scan_with_scanner(self.root, report_path=path)[3], 'unknown')

    def test_gateway_uses_current_cli_contract(self):
        emit = Mock()
        result = Mock(returncode=0, stdout=json.dumps({'ok': True, 'channels': {'discord': {'running': False}}}))
        with patch.object(compat.shutil, 'which', return_value='/trusted/openclaw'), patch.object(compat.subprocess, 'run', return_value=result) as run:
            self.assertEqual(compat.gateway_health(emit=emit, profile='work', timeout=2500), 0)
        self.assertEqual(run.call_args.args[0], ['/trusted/openclaw', '--profile', 'work', 'health', '--json', '--timeout', '2500'])
        self.assertEqual(run.call_args.kwargs['timeout'], 7.5)
        self.assertIn('does not prove channel health', emit.call_args.args[2][0])

    def test_gateway_failures_are_not_success(self):
        for result in (Mock(returncode=1, stdout='secret'), Mock(returncode=0, stdout='not json'), Mock(returncode=0, stdout='[]'), Mock(returncode=0, stdout='{"ok": false}')):
            emit = Mock()
            with patch.object(compat.shutil, 'which', return_value='/trusted/openclaw'), patch.object(compat.subprocess, 'run', return_value=result):
                self.assertEqual(compat.gateway_health(emit=emit), 2)
            self.assertNotIn('secret', str(emit.call_args))

    def test_gateway_timeout(self):
        with patch.object(compat.shutil, 'which', return_value='/trusted/openclaw'), patch.object(compat.subprocess, 'run', side_effect=subprocess.TimeoutExpired('mock', 1)):
            self.assertEqual(compat.gateway_health(emit=Mock()), 2)

    def test_missing_cli(self):
        with patch.object(compat.shutil, 'which', return_value=None), patch.object(compat.subprocess, 'run') as run:
            self.assertEqual(compat.gateway_health(emit=Mock()), 2)
            run.assert_not_called()

    def test_default_health_does_not_probe_cdp(self):
        args = app.build_parser().parse_args(['openclaw-health'])
        with patch.object(app, 'gateway_health', return_value=0) as gateway, patch.object(app, 'fetch_json') as cdp:
            self.assertEqual(args.func(args), 0)
            gateway.assert_called_once()
            cdp.assert_not_called()

    def test_legacy_endpoint_is_explicit_browser_only(self):
        args = app.build_parser().parse_args(['openclaw-health', '--endpoint', 'http://127.0.0.1:9000/json/version'])
        with patch.object(app, 'fetch_json', side_effect=[{'Browser': 'test'}, [{}]]) as fetch, patch.object(app, 'gateway_health') as gateway:
            self.assertEqual(args.func(args), 0)
            gateway.assert_not_called()
        self.assertEqual([x.args[0] for x in fetch.call_args_list], ['http://127.0.0.1:9000/json/version', 'http://127.0.0.1:9000/json/list'])
        self.assertIn('Browser-only', self.output.getvalue())

    def test_browser_requires_explicit_endpoint(self):
        with self.assertRaises(SystemExit):
            app.main(['browser-health'])

    def test_uv_uses_explicit_reviewed_project(self):
        project = self.root / 'scanner'
        project.mkdir()
        with patch.dict(os.environ, {'SKILL_SCANNER_DIR': str(project)}), patch.object(app.shutil, 'which', side_effect=lambda value: '/uv' if value == 'uv' else None):
            self.assertEqual(app.scanner_base(), ['uv', 'run', '--project', str(project), 'skill-scanner'])


if __name__ == '__main__':
    unittest.main()
