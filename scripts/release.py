"""Validate release evidence and publish a signed, metadata-only pointer."""

import argparse
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

PRIVATE = 'ivanovanatolyi1-rgb/moysklad-analytics'
PRIVATE_ID = 1433526963
PRIVATE_WORKFLOW = 430561998
PUBLIC = 'ivanovanatolyi1-rgb/gosha-release-control'
OWNER_ID = 345363067
NAMESPACE = 'gosha-release-v1'
SHA = re.compile(r'[0-9a-f]{40}\Z')
REF = 'refs/heads/release/gosha'
TEST_STEP = 'Run python -m unittest discover -s tests -v'


def api(path, token):
    # Never print private run records, logs, artifacts or HTTP response bodies.
    request = urllib.request.Request('https://api.github.com/' + path, headers={
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def verify_ci(sha, token, fetch=api):
    if not SHA.fullmatch(sha) or not token:
        raise ValueError('A full SHA and private CI read access are required')
    result = fetch(f'repos/{PRIVATE}/actions/workflows/{PRIVATE_WORKFLOW}/runs?'
                   f'head_sha={sha}&branch=feature/gosha-telegram&event=push&per_page=10', token)
    for run in result['workflow_runs']:
        if (run.get('workflow_id') != PRIVATE_WORKFLOW or run.get('head_sha') != sha
                or run.get('event') != 'push' or run.get('head_branch') != 'feature/gosha-telegram'
                or run.get('status') != 'completed' or run.get('conclusion') != 'success'
                or (run.get('repository') or {}).get('id') != PRIVATE_ID
                or (run.get('head_repository') or {}).get('id') != PRIVATE_ID):
            continue
        run_id, attempt = int(run['id']), int(run['run_attempt'])
        data = fetch(f'repos/{PRIVATE}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100', token)
        if any(job.get('name') == 'test' and job.get('conclusion') == 'success'
               and any(step.get('name') == TEST_STEP and step.get('conclusion') == 'success'
                       for step in job.get('steps', [])) for job in data['jobs']):
            return
    raise ValueError('Private CI has no successful test job for this exact SHA')


def verify_approval(run_id, token, fetch=api):
    reviews = fetch(f'repos/{PUBLIC}/actions/runs/{run_id}/approvals', token)
    owner_decisions = [review.get('state') for review in reviews
                       if (review.get('user') or {}).get('id') == OWNER_ID
                       and any(item.get('name') == 'gosha-production' for item in review.get('environments', []))]
    if not owner_decisions or any(decision != 'approved' for decision in owner_decisions):
        raise ValueError('Owner approval is missing or not final for this run and environment')


def canonical(manifest):
    return (json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(',', ':')) + '\n').encode()


def git(*args, env=None, data=None):
    return subprocess.run(['git', *args], env=env, input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, check=True, timeout=30).stdout.strip()


def envelope(sha, run_id, sequence, pem, public):
    if not SHA.fullmatch(sha) or not all(0 < value < 2**63 for value in (run_id, sequence)):
        raise ValueError('Invalid release identity')
    if not pem or not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+=*', public):
        raise ValueError('Release signing key is not configured')
    manifest = {'version': 1, 'repository': PRIVATE, 'repository_id': PRIVATE_ID,
                'sha': sha, 'run_id': run_id, 'sequence': sequence}
    with tempfile.TemporaryDirectory() as directory:
        key = Path(directory) / 'key'
        key.touch(mode=0o600)
        key.write_text(pem.rstrip('\n') + '\n')
        data = Path(directory) / 'manifest'
        data.write_bytes(canonical(manifest))
        def ssh(*args):
            return subprocess.run(['ssh-keygen', *args], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  check=True, timeout=15).stdout
        derived = b' '.join(ssh('-y', '-P', '', '-f', key).split()[:2]).decode('ascii')
        if derived != public:
            raise ValueError('Private and public signing keys differ')
        ssh('-Y', 'sign', '-f', key, '-n', NAMESPACE, data)
        signature = data.with_suffix('.sig').read_text(encoding='ascii')
    return canonical({'manifest': manifest, 'signature': signature})


def publish(data):
    # A standalone data branch with exactly one public JSON file. No private clone,
    # history, refs, objects or test output are copied to the public repository.
    parent = git('ls-remote', '--refs', 'origin', REF).split()
    if parent:
        git('fetch', '--no-tags', 'origin', REF)
        old = git('rev-parse', 'FETCH_HEAD').decode()
        previous = json.loads(git('show', old + ':current.json'))['manifest']
        if previous['sequence'] >= json.loads(data)['manifest']['sequence']:
            raise ValueError('An equal or newer release has already been published')
    with tempfile.TemporaryDirectory() as directory:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(directory) / 'index')}
        blob = git('hash-object', '-w', '--stdin', data=data).decode()
        git('read-tree', '--empty', env=env)
        git('update-index', '--add', '--cacheinfo', f'100644,{blob},current.json', env=env)
        tree = git('write-tree', env=env).decode()
        parents = ['-p', old] if parent else []
        commit = git('-c', 'user.name=Gosha release', '-c', 'user.email=gosha-release@users.noreply.github.com',
                     'commit-tree', tree, *parents, '-m', 'Publish signed Gosha release metadata').decode()
    git('push', 'origin', f'{commit}:{REF}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('test', 'approval', 'publish'))
    args = parser.parse_args()
    try:
        if args.operation == 'test':
            verify_ci(os.environ['REQUEST_SHA'], os.environ.get('GOSHA_PRIVATE_CI_TOKEN', ''))
            print('Private CI verified for the requested SHA')
        elif args.operation == 'approval':
            verify_approval(int(os.environ['CONTROL_RUN_ID']), os.environ['GH_TOKEN'])
            print('Owner approval verified for the current public workflow run')
        else:
            if os.environ['REQUEST_SHA'] != os.environ['TESTED_SHA']:
                raise ValueError('Approved SHA and tested SHA differ')
            publish(envelope(os.environ['TESTED_SHA'], int(os.environ['CONTROL_RUN_ID']),
                             int(os.environ['CONTROL_SEQUENCE']), os.environ.pop('SIGNING_KEY', ''),
                             os.environ.get('SIGNING_PUBLIC', '')))
            print('Signed manifest published')
    except (KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
        # Some JSON exceptions contain remote response data. Never print the exception.
        parser.exit(1, f'Release check failed ({type(exc).__name__}). Inspect configuration and CI status.\n')


if __name__ == '__main__':
    main()
