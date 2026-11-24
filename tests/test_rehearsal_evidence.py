"""Isolated branch/SHA evidence retains private registry and execution identities."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest

import yaml
import test_ci_evidence as fixture


class RehearsalEvidence(unittest.TestCase):
    setUp = fixture.EvidenceCLI.setUp
    command = fixture.EvidenceCLI.command
    cli = fixture.EvidenceCLI.cli
    images = fixture.EvidenceCLI.images

    def context(self, repository='aci-containers'):
        return dict(self.env, CI_REHEARSAL='1', GITHUB_EVENT_NAME='workflow_dispatch',
            GITHUB_REPOSITORY='anmol372/' + repository, GITHUB_REF='refs/heads/anmrasto/gha-rehearsal-20261119',
            GITHUB_SHA='c' * 40, TRAVIS_TAG='6.1.1.8')

    def identity_args(self, repository='aci-containers'):
        return ['--release-tag', '6.1.1.8', '--source-repository', 'anmol372/' + repository,
            '--source-sha', 'a' * 40, '--source-ref', 'candidate/runtime', '--run-id', '123',
            '--run-url', 'https://example.test/run', '--upstream-id', '42']

    def test_private_receipts_and_refresh_keep_actual_branch_identity_and_fixed_write_target(self):
        metadata = self.images()
        row = json.loads(metadata.read_text())
        digest = 'sha256:' + hashlib.sha256(b'quay.io/anmrasto/image-0:6.1.1.8.42').hexdigest()
        row.update(registry='quay.io/anmrasto', scan_policy='advisory', publication={'state': 'complete', 'references': [
            {'registry': 'rehearsal', 'reference': 'quay.io/anmrasto/image-0:' + tag, 'tag': tag, 'digest': digest}
            for tag in ('6.1.1.8.42.z', '6.1.1.8.42.111226.123')]},
            scan={'image': {'state': 'ok', 'digest': row['sha'], 'counts': dict.fromkeys('CHMLU', 0)},
                  'base': {'state': 'ok', 'digest': 'sha256:' + 'f' * 64, 'base_repository_digest': 'sha256:' + 'f' * 64, 'counts': dict.fromkeys('CHMLU', 0)}})
        metadata.write_text(json.dumps(row))
        bundle = self.root / 'private-images'
        self.cli('prepare-images', '--metadata', metadata, '--bundle', bundle, *self.identity_args(), env=self.context())
        manifest = json.loads((bundle / 'manifest.json').read_text())
        image = manifest['records'][0]
        self.assertEqual(image['quay'], []); self.assertEqual(image['docker'], [])
        self.assertEqual(len(image['rehearsal']), 2)
        self.assertNotIn('trigger_tag', manifest['run'])
        self.assertEqual(manifest['run']['source_ref'], 'candidate/runtime')
        self.assertEqual(manifest['rehearsal']['workflow_sha'], 'c' * 40)
        checkout = self.root / 'status'; shutil.copytree(bundle / 'artifacts', checkout)
        status = checkout / 'docs/release_artifacts/releases.yaml'
        status.write_text(yaml.safe_dump({'releases': [{'release_tag': '6.1.1.8', 'release_streams': [
            {'release_name': '6.1.1.8.z', 'container_images': [image], 'acc_provision': []},
            {'release_name': '6.1.1.8', 'container_images': [image], 'acc_provision': [{'tag': '6.1.1.8', 'origin': 'retained'}], 'released': False}]}]}))
        refresh = self.root / 'private-refresh'
        self.cli('prepare-image-refresh', '--status-checkout', checkout, '--bundle', refresh,
            '--mode', 'release', '--expected-images', 'image-0', *self.identity_args('acc-provision'), env=self.context('acc-provision'))
        refreshed = json.loads((refresh / 'manifest.json').read_text())
        self.assertEqual(refreshed['records'][0]['rehearsal'][0]['tag'], '6.1.1.8.42')
        self.assertEqual(refreshed['records'][0]['quay'], [])
        self.assertNotIn('tag_object', refreshed['run'])
        args = ['publish', '--bundle', refresh, '--repository', '/tmp/forbidden-production-transport', '--branch', 'main']
        for key, value in [('release-tag', '6.1.1.8'), ('kind', 'image-refresh'), ('stream', '6.1.1.8'),
                ('source-repository', 'anmol372/acc-provision'), ('source-sha', 'a' * 40), ('cicd-sha', refreshed['cicd']['sha']),
                ('run-id', '123'), ('images', 'image-0')]:
            args += ['--expected-' + key, value]
        result = self.cli(*args, env=self.context('acc-provision'), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fixed test status target', result.stderr)

    def test_source_ref_cannot_bypass_normal_identity_or_wrong_execution_context(self):
        metadata = self.images()
        for env in (self.env, dict(self.context(), GITHUB_EVENT_NAME='push'), dict(self.context(), GITHUB_REF='refs/heads/master')):
            result = self.cli('prepare-images', '--metadata', metadata, '--bundle', self.root / 'invalid',
                *self.identity_args(), env=env, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / 'invalid').exists())


if __name__ == '__main__':
    unittest.main()
