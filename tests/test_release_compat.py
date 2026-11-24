"""Release compatibility through real offline CLI and local Git transports."""
import copy
import hashlib
import json
from pathlib import Path
import re
import unittest

import yaml
import test_ci_evidence as fixture


class ReleaseCompat(unittest.TestCase):
    setUp = fixture.EvidenceCLI.setUp
    command = fixture.EvidenceCLI.command
    cli = fixture.EvidenceCLI.cli
    git = fixture.EvidenceCLI.git
    remote = fixture.EvidenceCLI.remote
    images = fixture.EvidenceCLI.images
    prepare = fixture.EvidenceCLI.prepare
    publish = fixture.EvidenceCLI.publish

    def expected_args(self, bundle):
        args = fixture.EvidenceCLI.expected_args(self, bundle)
        manifest = json.loads((bundle / 'manifest.json').read_text())
        if manifest['kind'] == 'image-refresh':
            args += ['--expected-tag-object', 'd' * 40]
        if manifest.get('package_only'):
            args[-1] = ','.join(manifest['existing_images']['names'])
        return args

    def seed(self):
        bundle = self.prepare()
        repo, checkout = self.remote()
        self.publish(bundle, repo)
        self.git('pull', '--ff-only', cwd=checkout)
        path = checkout / 'docs/release_artifacts/releases.yaml'
        data = yaml.safe_load(path.read_text())
        release = data['releases'][-1]
        image = release['release_streams'][0]['container_images'][0]
        for key, registry in [('quay', 'quay.io/noiro'), ('docker', 'noiro')]:
            image[key][0]['sha'] = hashlib.sha256(f'{registry}/image-0:6.1.1.8.42'.encode()).hexdigest()
        stable = next(row for row in release['release_streams'] if row['release_name'] == '6.1.1.8')
        stable.update(acc_provision=[{'tag': '6.1.1.8', 'link': 'https://pypi.org/project/acc-provision/6.1.1.8/#files',
            'commit': [{'sha': 'f' * 40}], 'build-time': 'original', 'custom': {'keep': True}}], released=False,
            custom='preserved', container_images=copy.deepcopy([image]))
        path.write_text(yaml.safe_dump(data))
        self.git('add', '.', cwd=checkout)
        self.git('commit', '-m', 'legacy known package and approved aliases', cwd=checkout)
        self.git('push', 'origin', 'main', cwd=checkout)
        return repo, checkout, stable

    def refresh(self, checkout, name='refresh', run_id='224'):
        bundle = self.root / name
        common = list(self.common)
        common[common.index('--run-id') + 1] = run_id
        # Deliberately independent coordinator source, with no new package.
        common[common.index('--source-sha') + 1] = 'b' * 40
        self.cli('prepare-image-refresh', '--bundle', bundle, '--status-checkout', checkout,
                 '--mode', 'release', '--expected-images', 'image-0', '--tag-object', 'd' * 40, *common)
        return bundle

    def package(self, checkout, name='package', version='6.1.1.8'):
        artifact = self.root / (name + '.tar.gz')
        artifact.write_bytes(b'new independent candidate bytes')
        bundle = self.root / name
        common = list(self.common)
        common[common.index('--trigger-tag') + 1] = version
        self.cli('prepare-package', '--bundle', bundle, '--artifact', artifact,
                 '--mode', 'release', '--version', version, '--status-checkout', checkout,
                 '--registry', f'https://pypi.org/project/acc-provision/{version}/#files', *common)
        return bundle

    def read_stable(self, repo):
        data = yaml.safe_load(self.git('--git-dir', str(repo), 'show', 'main:docs/release_artifacts/releases.yaml'))
        return next(item for item in data['releases'][-1]['release_streams'] if item['release_name'] == '6.1.1.8')

    def test_refresh_retains_entire_package_readiness_and_custom_state_without_package_tools(self):
        repo, checkout, original = self.seed()
        bundle = self.refresh(checkout)
        manifest = json.loads((bundle / 'manifest.json').read_text())
        for forbidden in ('package', 'package_artifact', 'released', 'images', 'retained_files'):
            self.assertNotIn(forbidden, manifest)
        before_tools = (self.root / 'tools.jsonl').read_text()
        self.publish(bundle, repo)
        after = self.read_stable(repo)
        self.assertEqual(after['acc_provision'], original['acc_provision'])
        self.assertEqual(after['released'], original['released'])
        self.assertEqual(after['custom'], original['custom'])
        self.assertEqual(after['container_images'][0]['commit'], original['container_images'][0]['commit'])
        self.assertEqual(before_tools, (self.root / 'tools.jsonl').read_text())
        self.assertNotIn('ImageId:', self.git('--git-dir', str(repo), 'log', '-1', '--format=%B'))
        head = self.git('--git-dir', str(repo), 'rev-parse', 'main')
        self.publish(bundle, repo)
        self.assertEqual(head, self.git('--git-dir', str(repo), 'rev-parse', 'main'))

    def test_cross_kind_selection_blocks_old_retained_package_without_touching_reports(self):
        repo, checkout, original = self.seed()
        old = self.package(checkout, 'old-package')
        refresh = self.refresh(checkout)
        self.publish(refresh, repo)
        before = self.git('--git-dir', str(repo), 'rev-parse', 'main')
        result = self.publish(old, repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('image selection changed', result.stderr)
        self.assertEqual(before, self.git('--git-dir', str(repo), 'rev-parse', 'main'))
        self.git('pull', '--ff-only', cwd=checkout)
        new = self.package(checkout, 'new-package')
        self.publish(new, repo)

    def test_refresh_package_race_forbidden_fields_and_wrong_raw_object_fail(self):
        repo, checkout, original = self.seed()
        bundle = self.refresh(checkout)
        path = bundle / 'manifest.json'
        manifest = json.loads(path.read_text())
        for key in ('package', 'package_artifact', 'released', 'images', 'retained_files'):
            changed = copy.deepcopy(manifest); changed[key] = []
            path.write_text(json.dumps(changed))
            self.assertNotEqual(self.publish(bundle, repo, check=False).returncode, 0)
        path.write_text(json.dumps(manifest))
        args = self.expected_args(bundle); args[-1] = 'e' * 40
        self.assertNotEqual(self.cli('publish', '--bundle', bundle, '--repository', repo, '--branch', 'main', *args, check=False).returncode, 0)
        status = checkout / 'docs/release_artifacts/releases.yaml'
        data = yaml.safe_load(status.read_text())
        data['releases'][-1]['release_streams'][1]['acc_provision'][0]['custom'] = 'changed'
        status.write_text(yaml.safe_dump(data))
        self.git('add', '.', cwd=checkout); self.git('commit', '-m', 'concurrent package change', cwd=checkout)
        self.git('push', 'origin', 'main', cwd=checkout)
        result = self.publish(bundle, repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('selected package changed', result.stderr)

    def test_post_update_is_package_only_preserving_images_reports_readiness_and_selection(self):
        repo, checkout, original = self.seed()
        refresh = self.refresh(checkout)
        self.publish(refresh, repo)
        self.git('pull', '--ff-only', cwd=checkout)
        before = self.read_stable(repo)
        index_path = 'docs/release_artifacts/6.1.1.8/r/.ci-evidence.json'
        marker = json.loads(self.git('--git-dir', str(repo), 'show', 'main:' + index_path))['image_selections']
        bundle = self.package(checkout, 'post-package', '6.1.1.8.post1')
        manifest = json.loads((bundle / 'manifest.json').read_text())
        self.assertTrue(manifest['package_only']); self.assertEqual(manifest['records'], [])
        self.assertTrue(all('/acc-provision/' in item['path'] for item in manifest['files']))
        self.publish(bundle, repo)
        after = self.read_stable(repo)
        self.assertEqual(after['container_images'], before['container_images'])
        self.assertEqual(after['released'], before['released'])
        self.assertEqual(after['acc_provision'][0]['tag'], '6.1.1.8.post1')
        self.assertEqual(json.loads(self.git('--git-dir', str(repo), 'show', 'main:' + index_path))['image_selections'], marker)

    def test_route_failed_reused_known_and_new_version(self):
        repo, checkout, original = self.seed()
        output = self.root / 'route.json'
        common = ['package-status-route', '--status-checkout', checkout, '--release-tag', '6.1.1.8', '--mode', 'release', '--index', 'pypi', '--version', '6.1.1.8', '--output', output]
        self.cli(*common)
        self.assertEqual(json.loads(output.read_text())['kind'], 'image-refresh')
        receipt = self.root / 'receipt.json'
        receipt.write_text(json.dumps({'schema_version': 1, 'verified': True, 'reused_existing': True, 'package': {'version': '6.1.1.8', 'index': 'pypi'}}))
        self.cli(*common, '--receipt', receipt)
        self.assertEqual(json.loads(output.read_text())['kind'], 'image-refresh')
        value = json.loads(receipt.read_text()); value['package']['version'] += '.post1'; receipt.write_text(json.dumps(value))
        common[common.index('--version') + 1] += '.post1'
        self.cli(*common, '--receipt', receipt)
        self.assertEqual(json.loads(output.read_text())['kind'], 'package')

    def test_reused_same_version_cannot_replace_original_provenance_in_fresh_clone(self):
        repo, checkout, original = self.seed()
        bundle = self.package(checkout)
        manifest = json.loads((bundle / 'manifest.json').read_text())
        receipt = self.root / 'reused-receipt.json'
        receipt.write_text(json.dumps({'schema_version': 1, 'verified': True, 'reused_existing': True,
            'package': manifest['package_artifact'], 'index': 'https://pypi.org',
            'source_sha': manifest['source']['sha'], 'cicd_commit': manifest['cicd']['sha'],
            'tag': '6.1.1.8', 'tag_object': 'd' * 40, 'input_lock_sha256': 'e' * 64}))
        result = self.cli('publish', '--bundle', bundle, '--receipt', receipt,
            '--repository', repo, '--branch', 'main', *self.expected_args(bundle), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('known package provenance is immutable', result.stderr)
        self.assertEqual(self.read_stable(repo)['acc_provision'], original['acc_provision'])

    def test_later_refresh_replay_and_changed_same_attempt_are_safe(self):
        repo, checkout, original = self.seed()
        first = self.refresh(checkout, 'refresh-first', '224')
        self.publish(first, repo)
        self.git('pull', '--ff-only', cwd=checkout)
        second = self.refresh(checkout, 'refresh-second', '225')
        self.publish(second, repo)
        head = self.git('--git-dir', str(repo), 'rev-parse', 'main')
        self.publish(first, repo)
        self.assertEqual(head, self.git('--git-dir', str(repo), 'rev-parse', 'main'))
        path = second / 'manifest.json'
        manifest = json.loads(path.read_text()); manifest['records'][0]['custom'] = 'changed'
        path.write_text(json.dumps(manifest))
        result = self.publish(second, repo, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('different frozen bundle', result.stderr)

    def test_missing_package_incomplete_cohort_and_wrong_alias_fail_before_publish(self):
        repo, checkout, original = self.seed()
        bundle = self.refresh(checkout)
        manifest = json.loads((bundle / 'manifest.json').read_text())
        manifest['records'][0]['quay'][0]['sha'] = 'f' * 64
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
        self.assertNotEqual(self.publish(bundle, repo, check=False).returncode, 0)
        output = self.root / 'route-missing.json'
        self.assertNotEqual(self.cli('package-status-route', '--status-checkout', checkout,
            '--release-tag', '6.1.1.8', '--mode', 'rc', '--rc-number', '9', '--index', 'pypi',
            '--version', '6.1.1.8rc9', '--output', output, check=False).returncode, 0)
        self.assertFalse(output.exists())
        self.assertNotEqual(self.cli('prepare-image-refresh', '--bundle', self.root / 'incomplete',
            '--status-checkout', checkout, '--mode', 'release', '--tag-object', 'd' * 40,
            '--expected-images', 'image-0,image-1', *self.common, check=False).returncode, 0)

    def test_release_package_extraction_matches_prechange_golden_without_marker(self):
        repo, checkout, original = self.seed()
        bundle = self.package(checkout)
        def normalize(value):
            if isinstance(value, dict): return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, list): return [normalize(item) for item in value]
            if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (PST|PDT)', value): return '<timestamp>'
            return value
        manifest = normalize(json.loads((bundle / 'manifest.json').read_text()))
        manifest['cicd']['sha'] = '<helper-sha>'
        golden = Path(__file__).parent / 'fixtures/package-release-golden.json'
        self.assertEqual(manifest, json.loads(golden.read_text()))


if __name__ == '__main__':
    unittest.main()
