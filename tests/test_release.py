import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import release


class ReleaseTests(unittest.TestCase):
    sha = 'a' * 40

    def evidence(self, **changes):
        return {'id': 10, 'run_attempt': 1, 'head_sha': self.sha,
                'workflow_id': release.PRIVATE_WORKFLOW, 'conclusion': 'success',
                'status': 'completed', 'event': 'push', 'head_branch': 'feature/gosha-telegram',
                'repository': {'id': release.PRIVATE_ID}, 'head_repository': {'id': release.PRIVATE_ID},
                **changes}

    def jobs(self, result='success'):
        return {'jobs': [{'name': 'test', 'conclusion': 'success',
                          'steps': [{'name': release.TEST_STEP, 'conclusion': result}]}]}

    def test_ci_requires_exact_private_push_and_successful_unit_test_step(self):
        for change in ({'head_sha': 'b' * 40}, {'event': 'pull_request'}, {'conclusion': 'failure'},
                       {'head_repository': {'id': 1}}, {'workflow_id': 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                release.verify_ci(self.sha, 'fixture', Mock(return_value={'workflow_runs': [self.evidence(**change)]}))
        fetch = Mock(side_effect=[{'workflow_runs': [self.evidence()]}, self.jobs('skipped')])
        with self.assertRaises(ValueError):
            release.verify_ci(self.sha, 'fixture', fetch)
        fetch = Mock(side_effect=[{'workflow_runs': [self.evidence()]}, self.jobs()])
        release.verify_ci(self.sha, 'fixture', fetch)
        self.assertIn('attempts/1/jobs', fetch.call_args.args[0])

    def test_approval_is_bound_to_requested_run_owner_and_environment(self):
        valid = {'user': {'id': release.OWNER_ID}, 'state': 'approved',
                 'environments': [{'name': 'gosha-production'}]}
        for change in ({'user': {'id': 1}}, {'state': 'rejected'}, {'environments': [{'name': 'staging'}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                release.verify_approval(321, 'fixture', Mock(return_value=[{**valid, **change}]))
        fetch = Mock(return_value=[valid])
        release.verify_approval(321, 'fixture', fetch)
        self.assertTrue(fetch.call_args.args[0].endswith('/runs/321/approvals'))

    def test_ed25519_manifest_contains_only_releasable_metadata_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / 'secret'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', key], check=True)
            public = ' '.join(key.with_suffix('.pub').read_text().split()[:2])
            signed = release.envelope(self.sha, 345, 22, key.read_text(), public)
            parsed = json.loads(signed)
            manifest = parsed['manifest']
            self.assertEqual(manifest, {'version': 1, 'repository': release.PRIVATE,
                                        'repository_id': release.PRIVATE_ID, 'sha': self.sha,
                                        'run_id': 345, 'sequence': 22})
            self.assertNotIn(b'PRIVATE KEY', signed)
            signers = root / 'allowed_signers'
            signers.write_text('gosha-owner namespaces="gosha-release-v1" ' + public + '\n')
            signature = root / 'manifest.sig'
            signature.write_text(parsed['signature'])
            subprocess.run(['ssh-keygen', '-Y', 'verify', '-f', signers, '-I', 'gosha-owner',
                            '-n', release.NAMESPACE, '-s', signature], input=release.canonical(manifest),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    def test_publish_creates_only_manifest_in_standalone_data_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, origin = root / 'repo', root / 'origin.git'
            repo.mkdir()
            def command(*args, **kwargs):
                return subprocess.run(['git', *args], cwd=repo, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, check=True, **kwargs).stdout.strip()
            command('init', '-q')
            command('init', '-q', '--bare', origin)
            command('remote', 'add', 'origin', origin)
            def in_repo(*args, env=None, data=None):
                return command(*args, env=env, input=data)
            with patch.object(release, 'git', in_repo):
                release.publish(release.canonical({'manifest': {'sequence': 1}}))
                release.publish(release.canonical({'manifest': {'sequence': 2}}))
                with self.assertRaises(ValueError):
                    release.publish(release.canonical({'manifest': {'sequence': 1}}))
            self.assertEqual(command('--git-dir', origin, 'ls-tree', '--name-only', release.REF), b'current.json')
            self.assertEqual(command('--git-dir', origin, 'rev-list', '--count', release.REF), b'2')
