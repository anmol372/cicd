"""Manual input resolution uses local fake git, never network or write credentials."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'travis/rehearsal_inputs.py'
spec = importlib.util.spec_from_file_location('rehearsal_inputs', PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RehearsalInputs(unittest.TestCase):
    def run_inputs(self, **changes):
        fields = dict(source_ref='reviewed/topic', test_version='1.2.3.4', stream='z',
                      rc_number='', operation='runtime', base_digest='sha256:' + 'b' * 64)
        fields.update(changes)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            cli = ['--source-directory', directory, '--github-output', str(output)]
            for key, value in fields.items():
                cli.append('--' + key.replace('_', '-') + '=' + value)
            calls = []
            def git(_directory, *args):
                calls.append(args)
                return (fields['source_ref'] if module.SHA.fullmatch(fields['source_ref']) else 'a' * 40) if args[0] == 'rev-parse' else ''
            with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch',
                      'GITHUB_REPOSITORY': 'anmol372/opflex', 'GITHUB_REF': module.EXECUTION_REF}), patch.object(module, 'git', git):
                result = module.main(cli)
            return result, output.read_text() if output.exists() else '', calls

    def test_branch_and_digest_are_captured_once(self):
        result, output, calls = self.run_inputs()
        self.assertEqual(result, 0)
        self.assertIn('source_sha=' + 'a' * 40, output)
        self.assertIn('base_digest=sha256:' + 'b' * 64, output)
        self.assertEqual([call for call in calls if call[0] == 'fetch'],
                         [('fetch', '--no-tags', 'origin', 'refs/heads/reviewed/topic')])

    def test_full_sha_is_not_reinterpreted_as_a_branch(self):
        result, _, calls = self.run_inputs(source_ref='c' * 40)
        self.assertEqual(result, 0)
        self.assertIn(('fetch', '--no-tags', 'origin', 'c' * 40), calls)
        self.assertFalse(any(call[0] == 'check-ref-format' for call in calls))

    def test_rc_and_release_keep_numeric_image_release(self):
        for stream, number in [('rc', '2'), ('release', '')]:
            result, output, _ = self.run_inputs(stream=stream, rc_number=number)
            self.assertEqual(result, 0)
            self.assertIn('release_tag=1.2.3.4\n', output)
            self.assertIn(f'stream={stream}\n', output)

    def test_invalid_input_fails_before_fetch_or_output(self):
        for changes in [dict(test_version='1.2.3.4rc1'), dict(stream='rc'),
                        dict(rc_number='1'), dict(base_digest='latest'),
                        dict(source_ref='--upload-pack=bad'), dict(source_ref='topic\nsource_sha=evil')]:
            with self.subTest(changes=changes):
                result, output, calls = self.run_inputs(**changes)
                self.assertNotEqual(result, 0)
                self.assertEqual(output, '')
                self.assertFalse(any(call[0] == 'fetch' for call in calls))

    def test_base_and_package_operation_need_no_base_digest(self):
        for operation in ['base', 'publish-package', 'refresh-image-status']:
            result, _, _ = self.run_inputs(operation=operation, base_digest='')
            self.assertEqual(result, 0)

    def test_wrong_execution_context_rejected(self):
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'push',
                       'GITHUB_REPOSITORY': 'noironetworks/opflex', 'GITHUB_REF': module.EXECUTION_REF}):
            with self.assertRaises(ValueError):
                module.resolve(None)
