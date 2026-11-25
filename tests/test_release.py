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

    def discovery_evidence(self):
        return [
            {'object': {'sha': self.sha}},
            {'sha': self.sha, 'parents': [{}, {}]},
            [{'number': 9}],
            {'merged': True, 'merge_commit_sha': self.sha, 'base': {
                'ref': release.BRANCH, 'repo': {'id': release.PRIVATE_ID}},
             'merged_by': {'id': release.OWNER_ID}},
            {'sha': release.CI_BLOB},
            {'workflow_runs': [self.evidence()]},
            self.jobs(),
            {'object': {'sha': self.sha}},
        ]

    def test_discover_requires_owner_github_merge_pinned_ci_and_stable_head(self):
        fetch = Mock(side_effect=self.discovery_evidence())
        self.assertEqual(release.discover('fixture', fetch), (self.sha, 10))
        for index, bad in (
            (1, {'sha': self.sha, 'parents': [{}]}), (2, []),
            (3, {**self.discovery_evidence()[3], 'merged_by': {'id': 2}}),
            (3, {**self.discovery_evidence()[3], 'merge_commit_sha': 'b' * 40}),
            (4, {'sha': 'b' * 40}),
            (5, {'workflow_runs': [self.evidence(run_attempt=2)]}),
            (5, {'workflow_runs': [self.evidence(status='in_progress')]}),
            (5, {'workflow_runs': [self.evidence(conclusion='failure')]}),
            (7, {'object': {'sha': 'b' * 40}}),
        ):
            evidence = self.discovery_evidence()
            evidence[index] = bad
            with self.subTest(index=index, bad=bad), self.assertRaises(release.NotReady):
                release.discover('fixture', Mock(side_effect=evidence))

    def test_request_deduplicates_even_after_rejection_and_preserves_only_metadata(self):
        previous = [{'sha': self.sha, 'ci_run_id': 10, 'run_id': 20}]
        with patch.object(release, 'read_branch', return_value=('parent', previous)), patch.object(
                release, 'write_branch') as write, patch.object(
                release, 'api', return_value={'workflow_runs': []}), patch.object(
                release, 'previous_resolved'), patch.object(release, 'read_pointer', return_value={}):
            with self.assertRaises(release.NotReady):
                release.reserve(self.sha, 10, 21, 'private', 'public', automatic=False)
            write.assert_not_called()
            release.reserve('b' * 40, 11, 22, 'private', 'public', automatic=False)
        self.assertEqual(json.loads(write.call_args.args[2]),
                         [*previous, {'sha': 'b' * 40, 'ci_run_id': 11, 'run_id': 22}])

    def test_pending_or_uncertain_publication_blocks_new_request(self):
        entries = [{'run_id': 20}]
        with self.assertRaises(release.NotReady):
            release.previous_resolved(entries, 'token', Mock(return_value={'status': 'waiting'}))
        jobs = {'jobs': [{'steps': [{'name': 'Sign and publish the exact tested SHA',
                                    'started_at': 'fixture', 'conclusion': 'failure'}]}]}
        with patch.object(release, 'read_pointer', return_value={}):
            with self.assertRaises(release.NotReady):
                release.previous_resolved(entries, 'token', Mock(side_effect=[{'status': 'completed'}, jobs]))

    def test_reservation_blocks_an_existing_approval_and_non_descendants(self):
        with patch.object(release, 'read_branch', return_value=(None, None)), patch.object(
                release, 'api', return_value={'workflow_runs': [{'id': 20}]}), patch.object(release, 'write_branch') as write:
            with self.assertRaises(release.NotReady):
                release.reserve(self.sha, 10, 21, 'private', 'public', automatic=False)
        write.assert_not_called()
        with self.assertRaises(release.NotReady):
            release.verify_forward(self.sha, 'b' * 40, 'token', Mock(return_value={'status': 'diverged'}))

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
                release.write_branch(release.REQUEST_REF, 'requests.json', b'[1]', None)
                with self.assertRaises(subprocess.CalledProcessError):
                    release.write_branch(release.REQUEST_REF, 'requests.json', b'[2]', None)
                self.assertEqual(release.read_branch(release.REQUEST_REF, 'requests.json')[1], [1])
            self.assertEqual(command('--git-dir', origin, 'ls-tree', '--name-only', release.REF), b'current.json')
            self.assertEqual(command('--git-dir', origin, 'rev-list', '--count', release.REF), b'2')
