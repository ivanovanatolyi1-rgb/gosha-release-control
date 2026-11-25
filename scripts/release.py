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


BRANCH = 'feature/gosha-telegram'
CI_PATH = '.github/workflows/gosha.yml'
CI_BLOB = '84e361adc3cd2987b6c77d00cb32feebd9e5b2cd'
REQUEST_REF = 'refs/heads/requests/gosha'


class NotReady(ValueError):
    """No eligible request at this tick."""


def verify_ci(sha, token, fetch=api, *, automatic=False):
    if not SHA.fullmatch(sha) or not token:
        raise ValueError('A full SHA and private CI read access are required')
    result = fetch(f'repos/{PRIVATE}/actions/workflows/{PRIVATE_WORKFLOW}/runs?'
                   f'head_sha={sha}&branch={BRANCH}&event=push&per_page=10', token)
    for run in result['workflow_runs']:
        if ((automatic and run.get('run_attempt') != 1)
                or run.get('workflow_id') != PRIVATE_WORKFLOW or run.get('head_sha') != sha
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
            return run_id
    raise NotReady('Private CI has no successful test job for this exact SHA')


def branch_head(token, fetch):
    sha = fetch(f'repos/{PRIVATE}/git/ref/heads/{BRANCH}', token)['object']['sha']
    if not SHA.fullmatch(sha):
        raise ValueError('Invalid branch head')
    return sha


def discover(token, fetch=api):
    sha = branch_head(token, fetch)
    commit = fetch(f'repos/{PRIVATE}/commits/{sha}', token)
    if commit.get('sha') != sha or len(commit.get('parents', [])) != 2:
        raise NotReady('Head is not a merge commit')
    pulls = fetch(f'repos/{PRIVATE}/commits/{sha}/pulls?per_page=100', token)
    for summary in pulls:
        number = int(summary['number'])
        pr = fetch(f'repos/{PRIVATE}/pulls/{number}', token)
        base = pr['base']
        if (pr.get('merged') is True and pr.get('merge_commit_sha') == sha
                and base['ref'] == BRANCH and base['repo']['id'] == PRIVATE_ID
                and (pr.get('merged_by') or {}).get('id') == OWNER_ID):
            break
    else:
        raise NotReady('Head has no matching PR merged by the owner')
    file = fetch(f'repos/{PRIVATE}/contents/{CI_PATH}?ref={sha}', token)
    if file.get('sha') != CI_BLOB:
        raise NotReady('Private CI workflow changed; owner review required')
    ci = verify_ci(sha, token, fetch, automatic=True)
    if branch_head(token, fetch) != sha:
        raise NotReady('Private branch advanced during verification')
    return sha, ci


def verify_forward(previous, sha, token, fetch=api):
    if previous:
        if not SHA.fullmatch(previous):
            raise ValueError('Invalid release baseline')
        if previous == sha or fetch(f'repos/{PRIVATE}/compare/{previous}...{sha}?per_page=1',
                                    token).get('status') != 'ahead':
            raise NotReady('Candidate is not newer than the published release')


def previous_resolved(entries, token, fetch=api):
    if not entries:
        return
    run = entries[-1]['run_id']
    prefix = f'repos/{PUBLIC}/actions/runs/{run}'
    if fetch(prefix, token).get('status') != 'completed':
        raise NotReady('Previous release request is still pending')
    jobs = fetch(prefix + '/jobs?per_page=100', token)['jobs']
    for job in jobs:
        for step in job.get('steps', []):
            if (step.get('name') == 'Sign and publish the exact tested SHA'
                    and step.get('started_at') and step.get('conclusion') not in ('success', 'skipped')):
                # A signing step can fail after pushing. Require the signed-channel
                # pointer to confirm that exact run, or stop for owner inspection.
                if read_pointer().get('run_id') != run:
                    raise NotReady('Previous publication has an unresolved result')


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


def read_branch(ref, filename):
    if not git('ls-remote', '--refs', 'origin', ref):
        return None, None
    git('fetch', '--no-tags', 'origin', ref)
    parent = git('rev-parse', 'FETCH_HEAD').decode()
    return parent, json.loads(git('show', parent + ':' + filename))


def read_pointer():
    _, pointer = read_branch(REF, 'current.json')
    if pointer is None:
        return {}
    manifest = pointer['manifest']
    public = os.environ['SIGNING_PUBLIC']
    if (not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+=*', public)
            or manifest.get('repository_id') != PRIVATE_ID or manifest.get('repository') != PRIVATE
            or not SHA.fullmatch(manifest.get('sha', ''))):
        raise ValueError('Invalid signed release pointer')
    with tempfile.TemporaryDirectory() as directory:
        signers, signature = (Path(directory) / name for name in ('signers', 'signature'))
        signers.write_text(f'gosha-owner namespaces="{NAMESPACE}" {public}\n')
        signature.write_text(pointer['signature'], encoding='ascii')
        subprocess.run(['ssh-keygen', '-Y', 'verify', '-f', signers, '-I', 'gosha-owner',
                        '-n', NAMESPACE, '-s', signature], input=canonical(manifest),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10)
    return manifest


def reserve(sha, ci, run, private_token, public_token, *, automatic):
    parent, entries = read_branch(REQUEST_REF, 'requests.json')
    entries = [] if entries is None and parent is None else entries
    if (not isinstance(entries, list)
            or any(set(entry) != {'sha', 'ci_run_id', 'run_id'} or not SHA.fullmatch(entry['sha'])
                   or any(type(entry[key]) is not int or entry[key] <= 0 for key in ('ci_run_id', 'run_id'))
                   for entry in entries)):
        raise ValueError('Invalid request ledger')
    if any(entry['sha'] == sha for entry in entries):
        raise NotReady('This SHA has already been requested')
    previous_resolved(entries, public_token)
    for status in ('waiting', 'pending'):
        runs = api(f'repos/{PUBLIC}/actions/workflows/gosha-control.yml/runs?status={status}&per_page=100',
                   public_token)['workflow_runs']
        if any(item['id'] != run for item in runs):
            raise NotReady('Another public release is waiting')
    verify_forward(read_pointer().get('sha'), sha, private_token)
    if automatic and branch_head(private_token, api) != sha:
        raise NotReady('Private branch advanced before reservation')
    data = canonical([*entries, {'sha': sha, 'ci_run_id': ci, 'run_id': run}])
    # No force push: a racer using the same old state cannot reach approval.
    write_branch(REQUEST_REF, 'requests.json', data, parent)


def publish(data):
    parent, previous = read_branch(REF, 'current.json')
    if previous and previous['manifest']['sequence'] >= json.loads(data)['manifest']['sequence']:
        raise ValueError('An equal or newer release has already been published')
    write_branch(REF, 'current.json', data, parent)


def write_branch(ref, filename, data, parent):
    with tempfile.TemporaryDirectory() as directory:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(directory) / 'index')}
        blob = git('hash-object', '-w', '--stdin', data=data).decode()
        git('read-tree', '--empty', env=env)
        git('update-index', '--add', '--cacheinfo', f'100644,{blob},{filename}', env=env)
        tree = git('write-tree', env=env).decode()
        parents = ['-p', parent] if parent else []
        commit = git('-c', 'user.name=Gosha release', '-c', 'user.email=gosha-release@users.noreply.github.com',
                     'commit-tree', tree, *parents, '-m', 'Publish signed Gosha release metadata').decode()
    git('push', 'origin', f'{commit}:{ref}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('test', 'request', 'approval', 'publish'))
    args = parser.parse_args()
    try:
        if args.operation == 'test':
            verify_ci(os.environ['REQUEST_SHA'], os.environ.get('GOSHA_PRIVATE_CI_TOKEN', ''))
            print('Private CI verified for the requested SHA')
        elif args.operation == 'request':
            private_token = os.environ['GOSHA_PRIVATE_CI_TOKEN']
            automatic = os.environ['REQUEST_OPERATION'] != 'release'
            if not automatic:
                sha = os.environ['REQUEST_SHA']
                ci = verify_ci(sha, private_token)
            else:
                sha, ci = discover(private_token)
            reserve(sha, ci, int(os.environ['CONTROL_RUN_ID']), private_token, os.environ['GH_TOKEN'],
                    automatic=automatic)
            with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
                output.write(f'sha={sha}\n')
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
    except NotReady as exc:
        if args.operation != 'request':
            parser.exit(1, 'Requested SHA has no eligible private CI.\n')
        print(f'No release request: {exc}')
    except (KeyError, ValueError, TypeError, OSError, subprocess.SubprocessError) as exc:
        # Some JSON exceptions contain remote response data. Never print the exception.
        parser.exit(1, f'Release check failed ({type(exc).__name__}). Inspect configuration and CI status.\n')


if __name__ == '__main__':
    main()
