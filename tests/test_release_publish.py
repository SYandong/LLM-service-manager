# Generated-By: Codex / gpt-6-astra
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

spec = importlib.util.spec_from_file_location('release_publish', Path(__file__).parents[1] / 'release/publish.py')
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)
COMMIT = 'a' * 40
HEAD = 'b' * 40
TAG = 'v0.1.0-alpha.9'


def event():
    return {'repository': {'full_name': pub.REPO}, 'workflow_run': {
        'id': 42, 'head_repository': {'full_name': pub.REPO}, 'event': 'push',
        'head_branch': 'main', 'name': 'ci', 'path': '.github/workflows/ci.yml',
        'conclusion': 'success', 'status': 'completed', 'head_sha': COMMIT}}


def test_successful_main_event_only():
    assert pub.validate_event(event()) == COMMIT


@pytest.mark.parametrize('key,value', [('event', 'pull_request'), ('event', 'workflow_dispatch'),
    ('head_branch', 'attacker'), ('name', 'lookalike'), ('path', '.github/workflows/evil.yml'),
    ('conclusion', 'failure'), ('status', 'in_progress'), ('head_sha', 'main'),
    ('head_repository', {'full_name': 'attacker/fork'})])
def test_untrusted_or_incomplete_events_rejected(key, value):
    e = event(); e['workflow_run'][key] = value
    with pytest.raises(ValueError):
        pub.validate_event(e)


def review(head=HEAD, state='COMMENTED', body=None, at='2026-09-10T00:00:00Z'):
    return {'user': {'login': pub.FABLE_LOGIN}, 'commit_id': head, 'state': state,
            'body': body or f'FABLE-APPROVED {head}\n\nGenerated-By: Claude Code / claude-fable-5-1',
            'submitted_at': at, 'html_url': 'https://example.invalid/review'}


def test_shared_account_commented_exact_fable_is_valid():
    assert pub.approval([review()], HEAD).endswith('/review')


@pytest.mark.parametrize('reviews', [[], [review(head=COMMIT)], [review(state='CHANGES_REQUESTED')],
    [review(body='FABLE-APPROVED ' + HEAD)],
    [review(), review(state='CHANGES_REQUESTED', at='2026-09-11T00:00:00Z')],
    [dict(review(), user={'login': 'attacker'})]])
def test_stale_missing_or_revoked_review_rejected(reviews):
    with pytest.raises(ValueError):
        pub.approval(reviews, HEAD)


def assets(path):
    path.mkdir()
    m = {'commit': COMMIT, 'tag': TAG, 'python_version': '0.1.0a9', 'assets': {}}
    for name in ['llm', 'llmsvc-0.1.0a9-py3-none-any.whl', 'llmsvc-0.1.0a9.tar.gz', 'deployment.tar.gz']:
        f = path / name; f.write_bytes(name.encode())
        m['assets'][name] = {'sha256': pub.sha256(f), 'bytes': f.stat().st_size}
    (path / 'release-manifest.json').write_text(json.dumps(m))
    (path / 'SHA256SUMS').write_text(''.join(pub.sha256(f) + '  ' + f.name + '\n' for f in sorted(path.iterdir()) if f.name != 'SHA256SUMS'))
    return m


def test_manifest_complete_set(tmp_path):
    assets(tmp_path / 'a')
    assert pub.validate_assets(tmp_path / 'a', COMMIT, TAG)['commit'] == COMMIT


@pytest.mark.parametrize('damage', ['bytes', 'missing', 'extra', 'duplicate', 'identity', 'symlink'])
def test_corrupt_or_ambiguous_remote_assets_fail_closed(tmp_path, damage):
    d = tmp_path / 'a'; assets(d)
    if damage == 'bytes': (d / 'llm').write_text('changed')
    if damage == 'missing': (d / 'llm').unlink()
    if damage == 'extra': (d / 'extra').write_text('unexpected')
    if damage == 'duplicate':
        f = d / 'SHA256SUMS'; f.write_text(f.read_text() + f.read_text().splitlines()[0] + '\n')
    if damage == 'identity':
        f = d / 'release-manifest.json'; m = json.loads(f.read_text()); m['commit'] = HEAD; f.write_text(json.dumps(m))
    if damage == 'symlink':
        (tmp_path / 'llm').write_bytes((d / 'llm').read_bytes()); (d / 'llm').unlink(); (d / 'llm').symlink_to(tmp_path / 'llm')
    with pytest.raises(ValueError): pub.validate_assets(d, COMMIT, TAG)


@pytest.mark.parametrize('draft', [False, True])
def test_repeat_publication_and_complete_draft_resume_without_build(tmp_path, monkeypatch, draft):
    saved = tmp_path / 'saved'; assets(saved)
    root = tmp_path / 'run'; root.mkdir(); calls = []
    monkeypatch.setattr(pub, 'pages', lambda _: [{'tag_name': TAG, 'prerelease': True, 'draft': draft}])
    monkeypatch.setattr(pub, 'build', lambda *a: pytest.fail('must reuse existing verified release'))
    def run(*args, **kwargs):
        calls.append(args)
        if args[:2] == ('git', 'ls-remote'): return COMMIT + '\trefs/tags/' + TAG
        if args[:2] == ('git', 'rev-parse'): return COMMIT
        if args[:3] == ('gh', 'release', 'download'):
            for f in saved.iterdir(): shutil.copy2(f, root / 'existing' / f.name)
        return ''
    monkeypatch.setattr(pub, 'run', run)
    pub.publish({'tag': TAG, 'commit': COMMIT}, root)
    assert not any('push' in c or 'create' in c for c in calls)
    assert sum(c[:3] == ('gh', 'release', 'edit') for c in calls) == int(draft)


def test_partial_draft_never_publishes_or_overwrites(tmp_path, monkeypatch):
    saved = tmp_path / 'saved'; assets(saved); (saved / 'llm').unlink()
    root = tmp_path / 'run'; root.mkdir(); calls = []
    monkeypatch.setattr(pub, 'pages', lambda _: [{'tag_name': TAG, 'prerelease': True, 'draft': True}])
    monkeypatch.setattr(pub, 'build', lambda *a: pytest.fail('no replacing draft bytes'))
    def run(*args, **kwargs):
        calls.append(args)
        if args[:2] == ('git', 'ls-remote'): return COMMIT
        if args[:2] == ('git', 'rev-parse'): return COMMIT
        if args[:3] == ('gh', 'release', 'download'):
            for f in saved.iterdir(): shutil.copy2(f, root / 'existing' / f.name)
        return ''
    monkeypatch.setattr(pub, 'run', run)
    with pytest.raises(ValueError): pub.publish({'tag': TAG, 'commit': COMMIT}, root)
    assert not any(c[:3] == ('gh', 'release', 'edit') for c in calls)


def test_normal_main_merge_skips_release(tmp_path, monkeypatch):
    monkeypatch.setattr(pub, 'api', lambda p: event()['workflow_run'] if '/actions/runs/' in p else [{
        'merged_at': 'now', 'merge_commit_sha': COMMIT, 'base': {'ref': 'main', 'repo': {'full_name': pub.REPO}},
        'title': 'Fix something (#42)'}])
    monkeypatch.setattr(pub, 'run', lambda *a, **kw: COMMIT)
    assert pub.guard(event()) is None


@pytest.fixture
def release_gate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'llmsvc').mkdir(); (tmp_path / 'cli').mkdir()
    (tmp_path / 'pyproject.toml').write_text('version = "0.1.0a9"\n')
    (tmp_path / 'llmsvc/__init__.py').write_text('__version__ = "0.1.0a9"')
    (tmp_path / 'cli/llm').write_text('version="0.1.0a9"')
    (tmp_path / 'CHANGELOG.md').write_text('# Changelog\n\n## 0.1.0-alpha.9 — 2026-09-10\nNotes')
    pr = {'number': 169, 'merged_at': 'now', 'merge_commit_sha': COMMIT,
          'base': {'ref': 'main', 'repo': {'full_name': pub.REPO}},
          'head': {'sha': HEAD, 'repo': {'full_name': pub.REPO}},
          'title': 'chore(release): ' + TAG, 'body': '', 'html_url': 'https://example.invalid/169'}
    state = {'pr': pr, 'reviews': [review()], 'ci': 'success', 'threads': [], 'log': '\n'.join(f'Change (#{n})' for n in range(5)), 'runs': []}
    def api(path):
        if '/actions/runs/' in path: return event()['workflow_run'] | {'html_url': 'https://example.invalid/ci'}
        if '/commits/' in path: return [pr]
        if path.endswith('/pulls/169'): return pr
        if '/workflows/ci.yml/runs' in path: return {'workflow_runs': [{'id': 1, 'conclusion': state['ci']}]}
        pytest.fail(path)
    def pages(path):
        if path.endswith('/reviews'): return state['reviews']
        if path.endswith('/releases'): return [{'tag_name': 'v0.1.0-alpha.8', 'draft': False}]
        pytest.fail(path)
    def run(*args, **kwargs):
        state['runs'].append(args)
        if args[:3] == ('git', 'rev-parse', 'HEAD'): return COMMIT
        if args[:2] == ('git', 'rev-parse'): return 'c' * 40
        if args[:2] == ('git', 'log'): return state['log']
        if args[:3] == ('gh', 'api', 'graphql'):
            return json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {
                'pageInfo': {'hasNextPage': False}, 'nodes': state['threads']}}}}})
        return ''
    monkeypatch.setattr(pub, 'api', api); monkeypatch.setattr(pub, 'pages', pages); monkeypatch.setattr(pub, 'run', run)
    return state


def test_complete_release_guard_counts_only_non_release_prs(release_gate):
    release_gate['log'] += '\nchore(release): v0.1.0-alpha.9 (#169)'
    result = pub.guard(event())
    assert result['commit'] == COMMIT and result['reviewed_head'] == HEAD
    assert len(result['qualifying_prs']) == 5
    assert ('git', 'diff', '--exit-code', HEAD, COMMIT) in release_gate['runs']


@pytest.mark.parametrize('damage', ['ci', 'threads', 'cadence', 'review', 'fork', 'title', 'direct_push'])
def test_complete_guard_rejects_unmet_gates(release_gate, damage):
    if damage == 'ci': release_gate['ci'] = 'failure'
    if damage == 'threads': release_gate['threads'] = [{'isResolved': False}]
    if damage == 'cadence': release_gate['log'] = 'Fix (#1)'
    if damage == 'review': release_gate['reviews'] = [review(head=COMMIT)]
    if damage == 'fork': release_gate['pr']['head']['repo']['full_name'] = 'attacker/fork'
    if damage == 'title': release_gate['pr']['title'] = 'chore(release): v0.1.0-alpha.8'
    if damage == 'direct_push': release_gate['log'] += '\nDirect unreviewed change'
    with pytest.raises(ValueError): pub.guard(event())


def test_reviewed_explicit_exception_is_recorded(release_gate):
    release_gate['log'] = 'Deploy pipeline (#169)'
    release_gate['pr']['body'] = 'Release-Exception: #169 — Verify the user-authorized delivery pipeline now.'
    assert pub.guard(event())['cadence_exception'].startswith('Release-Exception: #169')
