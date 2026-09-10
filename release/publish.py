# Generated-By: Codex / gpt-6-astra
"""Trusted main-CI release publisher. No deployment or PR execution surface."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import zipfile

REPO = 'SYandong/LLM-service-manager'
FABLE_LOGIN = 'lushuyu'  # Shared account; exact harness/head marker is also required.
SHA = re.compile(r'[0-9a-f]{40}')
VERSION = re.compile(r'0\.1\.0a([1-9][0-9]*)')


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def api(path):
    return json.loads(run('gh', 'api', path))


def pages(path):
    result = []
    for page in range(1, 101):
        batch = api(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise ValueError('API pagination limit exceeded')


def validate_event(event):
    r = event['workflow_run']
    if not (event['repository']['full_name'] == REPO
            and r['head_repository']['full_name'] == REPO
            and r['event'] == 'push' and r['head_branch'] == 'main'
            and r['name'] == 'ci' and r['path'] == '.github/workflows/ci.yml'
            and r['conclusion'] == 'success' and r['status'] == 'completed'
            and SHA.fullmatch(r['head_sha'])):
        raise ValueError('Not a successful trusted main CI event')
    return r['head_sha']


def approval(reviews, head):
    fable = [r for r in reviews if r['user']['login'] == FABLE_LOGIN
             and 'Generated-By: Claude Code / claude-fable-5-1' in r['body']]
    if not fable:
        raise ValueError('Missing Fable review')
    latest = sorted(fable, key=lambda r: r['submitted_at'])[-1]
    if (latest['commit_id'] != head or latest['state'] not in ('APPROVED', 'COMMENTED')
            or not re.search(r'^FABLE-APPROVED ' + re.escape(head) + r'\s*$', latest['body'], re.M)):
        raise ValueError('Latest Fable review does not approve exact head')
    return latest['html_url']


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def validate_assets(directory, commit, tag):
    """No extraction/execution until the complete manifest/checksum set agrees."""
    m = json.loads((directory / 'release-manifest.json').read_text())
    if m['commit'] != commit or m['tag'] != tag:
        raise ValueError('Existing release identity differs')
    names = set(m['assets'])
    version = m['python_version']
    if not VERSION.fullmatch(version) or tag != 'v0.1.0-alpha.' + VERSION.fullmatch(version)[1]:
        raise ValueError('Invalid version/tag')
    expected = {'llm', f'llmsvc-{version}-py3-none-any.whl', f'llmsvc-{version}.tar.gz', 'deployment.tar.gz'}
    if names != expected:
        raise ValueError('Unexpected payload set')
    sums = {}
    for line in (directory / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ')
        if name in sums or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Invalid/duplicate checksum')
        sums[name] = digest
    if set(sums) != names | {'release-manifest.json'}:
        raise ValueError('Checksum set differs')
    if {p.name for p in directory.iterdir()} != set(sums) | {'SHA256SUMS'}:
        raise ValueError('Extra or missing asset')
    for name, digest in sums.items():
        p = directory / name
        if not p.is_file() or p.is_symlink() or sha256(p) != digest:
            raise ValueError('Asset hash mismatch: ' + name)
        if name in names and (m['assets'][name]['sha256'] != digest or m['assets'][name]['bytes'] != p.stat().st_size):
            raise ValueError('Manifest disagrees: ' + name)
    return m


def guard(event):
    commit = validate_event(event)
    # Re-fetch the run rather than trusting a hand-written event body.
    current = api(f'repos/{REPO}/actions/runs/{event["workflow_run"]["id"]}')
    if validate_event({'repository': event['repository'], 'workflow_run': current}) != commit:
        raise ValueError('CI run changed')
    if run('git', 'rev-parse', 'HEAD') != commit:
        raise ValueError('Checkout differs from CI commit')
    associated = api(f'repos/{REPO}/commits/{commit}/pulls')
    prs = [p for p in associated if p['merged_at'] and p['merge_commit_sha'] == commit
           and p['base']['ref'] == 'main' and p['base']['repo']['full_name'] == REPO]
    if not any(p['title'].startswith('chore(release): ') for p in prs):
        return None  # Normal main CI never triggers a release.
    if len(prs) != 1:
        raise ValueError('Ambiguous release PR association')
    pr = api(f'repos/{REPO}/pulls/{prs[0]["number"]}')
    head = pr['head']['sha']
    if pr['head']['repo']['full_name'] != REPO:
        raise ValueError('Release PR must be repository-owned')
    reviewed = approval(pages(f'repos/{REPO}/pulls/{pr["number"]}/reviews'), head)
    # Never carry a stale head CI or later failed rerun into publication.
    runs = api(f'repos/{REPO}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request&per_page=100')['workflow_runs']
    if not runs or max(runs, key=lambda r: r['id'])['conclusion'] != 'success':
        raise ValueError('Current release-head CI is not successful')
    query = 'query { repository(owner:"SYandong",name:"LLM-service-manager") { pullRequest(number:%d) { reviewThreads(first:100) { pageInfo { hasNextPage } nodes { isResolved } } } } }' % pr['number']
    threads = json.loads(run('gh', 'api', 'graphql', '-f', 'query=' + query))['data']['repository']['pullRequest']['reviewThreads']
    if threads['pageInfo']['hasNextPage'] or any(not t['isResolved'] for t in threads['nodes']):
        raise ValueError('Unresolved review threads')
    run('git', 'fetch', 'origin', head)
    run('git', 'diff', '--exit-code', head, commit)
    version = re.search(r'^version = "([^"]+)"', Path('pyproject.toml').read_text(), re.M)[1]
    match = VERSION.fullmatch(version)
    if not match or f'"{version}"' not in Path('llmsvc/__init__.py').read_text() or f'version="{version}"' not in Path('cli/llm').read_text():
        raise ValueError('Version literals differ')
    tag = 'v0.1.0-alpha.' + match[1]
    if pr['title'] != 'chore(release): ' + tag:
        raise ValueError('Release title/version differ')
    if not Path('CHANGELOG.md').read_text().split('\n## ', 2)[1].startswith(tag[1:] + ' — '):
        raise ValueError('Changelog version differs')
    releases = pages(f'repos/{REPO}/releases')
    prior = [r for r in releases if not r['draft'] and re.fullmatch(r'v0\.1\.0-alpha\.[1-9][0-9]*', r['tag_name']) and r['tag_name'] != tag]
    previous = max(prior, key=lambda r: int(r['tag_name'].rsplit('.', 1)[1]))
    if int(match[1]) != int(previous['tag_name'].rsplit('.', 1)[1]) + 1:
        raise ValueError('Release version is not the next alpha')
    baseline = run('git', 'rev-parse', previous['tag_name'] + '^{commit}')
    run('git', 'merge-base', '--is-ancestor', baseline, commit)
    batch = []
    for line in run('git', 'log', '--first-parent', '--format=%s', baseline + '..' + commit).splitlines():
        if line.startswith('chore(release): '):
            continue
        number = re.search(r'\(#(\d+)\)$', line)
        if not number:
            raise ValueError('Non-PR commit in release batch')
        batch.append(int(number[1]))
    # A reviewed explicit delivery exception is auditable, never inferred from date.
    exception = re.search(r'^Release-Exception: (#\d+) — (.+)$', pr['body'] or '', re.M)
    if len(batch) < 5 and not exception:
        raise ValueError('Five-PR cadence not reached; no reviewed exception')
    return {'tag': tag, 'python_version': version, 'commit': commit,
            'release_pr': pr['html_url'], 'reviewed_head': head, 'fable_review': reviewed,
            'previous_tag_commit': baseline, 'qualifying_prs': list(reversed(batch)),
            'cadence_exception': exception[0] if exception else None,
            'merge_ci': current['html_url'], 'generated_by': 'Codex / gpt-6-astra'}


def wheel_contents(path):
    with zipfile.ZipFile(path) as z:
        return {n: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()}


def build(evidence, root):
    import sys
    import platform
    if sys.version_info[:2] != (3, 10) or sys.platform != 'linux' or platform.machine() != 'x86_64':
        raise ValueError('Wheelhouse requires CPython3.10 Linux x86_64')
    artifacts = root / 'artifacts'; artifacts.mkdir()
    run(sys.executable, '-c', 'from setuptools.build_meta import build_sdist,build_wheel; build_sdist(%r); build_wheel(%r)' % (str(artifacts), str(artifacts)))
    wheel = next(artifacts.glob('*.whl')); sdist = next(artifacts.glob('*.tar.gz'))
    unpacked = root / 'sdist'; unpacked.mkdir()
    with tarfile.open(sdist) as t:
        t.extractall(unpacked, filter='data')
    rebuilt = root / 'rebuilt'; rebuilt.mkdir()
    run(sys.executable, '-c', 'from setuptools.build_meta import build_wheel; build_wheel(%r)' % str(rebuilt), cwd=next(unpacked.iterdir()))
    assert wheel_contents(wheel) == wheel_contents(next(rebuilt.glob('*.whl')))
    wheelhouse = root / 'wheelhouse'; wheelhouse.mkdir()
    run(sys.executable, '-m', 'pip', 'download', '--only-binary=:all:', '--dest', str(wheelhouse), str(wheel) + '[tui]')
    shutil.copy2('cli/llm', artifacts / 'llm')
    run(sys.executable, '-m', 'pip', 'download', '--only-binary=:all:', '--no-deps', '--dest', str(wheelhouse), 'pip==26.2.1')
    bootstrap = next(wheelhouse.glob('pip-*.whl'))
    install_wheels = sorted(p for p in wheelhouse.glob('*.whl') if p != bootstrap)
    # Two independent offline installs use the exact bootstrap needed on site.
    for mode in ('minimal', 'tui'):
        env = root / mode
        run(sys.executable, '-m', 'venv', '--without-pip', str(env))
        python = str(env / 'bin/python')
        package = str(wheel) + ('[tui]' if mode == 'tui' else '')
        pip = ('env', 'PYTHONPATH=' + str(bootstrap), python, '-m', 'pip')
        run(*pip, 'install', '--no-index', '--find-links', str(wheelhouse), package)
        run(*pip, 'check')
        for command in ((python, '-I', '-c', 'import llmsvc;print(llmsvc.__version__)'),
                        (str(env / 'bin/llm'), '--version'), (str(env / 'bin/llmsvc-scheduler'), '--version')):
            assert run(*command, cwd=root) == evidence['python_version']
        code = 'import tui.app' if mode == 'tui' else "import importlib.util;assert importlib.util.find_spec('textual') is None"
        run(python, '-I', '-c', code, cwd=root)
    assert run(sys.executable, '-I', '-S', str(artifacts / 'llm'), '--version') == evidence['python_version']
    deployment = {'schema_version': 1, 'tag': evidence['tag'], 'version': evidence['python_version'],
                  'commit': evidence['commit'], 'scope': 'read_only',
                  'app_wheel': 'wheelhouse/' + wheel.name, 'cli': 'llm',
                  'bootstrap_pip': 'wheelhouse/' + bootstrap.name,
                  'install_wheels': ['wheelhouse/' + p.name for p in install_wheels],
                  'files': {'llm': sha256(artifacts / 'llm'),
                            **{'wheelhouse/' + p.name: sha256(p) for p in wheelhouse.iterdir()}}}
    (root / 'deployment.json').write_text(json.dumps(deployment, indent=2) + '\n')
    with tarfile.open(artifacts / 'deployment.tar.gz', 'w:gz') as t:
        t.add(root / 'deployment.json', arcname='deployment.json')
        t.add(artifacts / 'llm', arcname='llm')
        for p in sorted(wheelhouse.iterdir()):
            t.add(p, arcname='wheelhouse/' + p.name)
    evidence['wheelhouse'] = {'python': '3.10', 'platform': 'linux_x86_64',
                             'files': {p.name: sha256(p) for p in sorted(wheelhouse.iterdir())}}
    evidence['verification'] = {'wheel_sdist_entries_equal': len(wheel_contents(wheel)),
                                'offline_minimal_tui_installs': True, 'standalone_isolated_cli': True}
    evidence['assets'] = {p.name: {'sha256': sha256(p), 'bytes': p.stat().st_size} for p in artifacts.iterdir()}
    (artifacts / 'release-manifest.json').write_text(json.dumps(evidence, indent=2) + '\n')
    (artifacts / 'SHA256SUMS').write_text(''.join(sha256(p) + '  ' + p.name + '\n' for p in sorted(artifacts.iterdir()) if p.name != 'SHA256SUMS'))
    validate_assets(artifacts, evidence['commit'], evidence['tag'])
    return artifacts


def publish(evidence, root):
    tag, commit = evidence['tag'], evidence['commit']
    existing = [r for r in pages(f'repos/{REPO}/releases') if r['tag_name'] == tag]
    refs = run('git', 'ls-remote', 'origin', 'refs/tags/' + tag, 'refs/tags/' + tag + '^{}')
    if refs:
        run('git', 'fetch', 'origin', 'refs/tags/' + tag + ':refs/tags/' + tag)
        if run('git', 'rev-parse', tag + '^{commit}') != commit:
            raise ValueError('Immutable tag points elsewhere')
    if existing:
        if not refs:
            raise ValueError('Existing release has no immutable tag')
        remote = root / 'existing'; remote.mkdir()
        run('gh', 'release', 'download', tag, '--repo', REPO, '--dir', str(remote))
        validate_assets(remote, commit, tag)
        if not existing[0]['prerelease']:
            raise ValueError('Existing release is not a prerelease')
        if existing[0]['draft']:
            run('gh', 'release', 'edit', tag, '--repo', REPO, '--draft=false', '--latest=false')
        return  # Published release is immutable; complete draft resumes without rebuild.
    artifacts = build(evidence, root)
    if not refs:
        run('git', '-c', 'user.name=github-actions[bot]', '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
            'tag', '-a', tag, commit, '-m', tag + '\n\nGenerated-By: Codex / gpt-6-astra')
        run('git', 'push', 'origin', 'refs/tags/' + tag)
    notes = root / 'notes.md'
    changelog = Path('CHANGELOG.md').read_text().split('\n## ', 2)[1]
    notes.write_text(changelog.split('\n', 1)[1] + '\n\nExact commit: `' + commit + '`. Verify SHA256SUMS and release-manifest.json. Deployment is owned by the outbound host consumer under #169.\n\nGenerated-By: Codex / gpt-6-astra\n')
    run('gh', 'release', 'create', tag, *map(str, sorted(artifacts.iterdir())), '--repo', REPO, '--verify-tag', '--target', commit,
        '--draft', '--prerelease', '--title', tag, '--notes-file', str(notes))
    remote = root / 'downloaded'; remote.mkdir()
    run('gh', 'release', 'download', tag, '--repo', REPO, '--dir', str(remote))
    validate_assets(remote, commit, tag)
    if any(sha256(p) != sha256(remote / p.name) for p in artifacts.iterdir()):
        raise ValueError('Uploaded bytes differ')
    run('gh', 'release', 'edit', tag, '--repo', REPO, '--draft=false', '--latest=false')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--event', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    evidence = guard(json.loads(args.event.read_text()))
    if evidence is None:
        print('Not a release PR; no publication.')
        return
    args.work.mkdir(parents=True, exist_ok=False)
    publish(evidence, args.work.resolve())
    print('Verified prerelease: ' + evidence['tag'])


if __name__ == '__main__':
    main()
