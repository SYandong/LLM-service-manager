# Generated-By: Claude Code / claude-fable-5-1
"""Stable (non-alpha) tags through the same trusted publisher."""

import json

import pytest

from test_release_publish import COMMIT, event, pub, release_gate  # noqa: F401  (fixture re-exported)


@pytest.mark.parametrize('version,tag,prerelease', [
    ('0.1.0a9', 'v0.1.0-alpha.9', True), ('1.0.0', 'v1.0.0', False), ('1.2.3', 'v1.2.3', False),
])
def test_tag_mapping_covers_alpha_and_stable_versions(version, tag, prerelease):
    assert pub.tag_for(version) == tag
    assert pub.is_prerelease(tag) is prerelease
    assert pub.latest_flag(tag) == ('false' if prerelease else 'true')


@pytest.mark.parametrize('version', ['1.0', '01.0.0', '1.0.0a1', 'v1.0.0', '0.1.0a0'])
def test_invalid_versions_have_no_tag(version):
    with pytest.raises(ValueError):
        pub.tag_for(version)


def test_every_alpha_orders_before_every_stable_release():
    assert (pub.order_key('v0.1.0-alpha.15') < pub.order_key('v0.1.0')
            < pub.order_key('v1.0.0') < pub.order_key('v1.0.1') < pub.order_key('v1.1.0'))


def stable_files(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('version = "1.0.0"\n')
    (tmp_path / 'llmsvc/__init__.py').write_text('__version__ = "1.0.0"')
    (tmp_path / 'cli/llm').write_text('version="1.0.0"')
    (tmp_path / 'CHANGELOG.md').write_text('# Changelog\n\n## 1.0.0 — 2026-09-15\nNotes')


def test_stable_release_after_alpha_series_is_accepted(release_gate, tmp_path):
    stable_files(tmp_path)
    release_gate['pr']['title'] = 'chore(release): v1.0.0'
    result = pub.guard(event())
    assert result['tag'] == 'v1.0.0' and result['python_version'] == '1.0.0'


def test_stable_release_must_advance_the_published_series(release_gate, tmp_path, monkeypatch):
    stable_files(tmp_path)
    release_gate['pr']['title'] = 'chore(release): v1.0.0'
    monkeypatch.setattr(pub, 'pages', lambda path: release_gate['reviews'] if path.endswith('/reviews')
                        else [{'tag_name': 'v1.0.0', 'draft': False}, {'tag_name': 'v1.1.0', 'draft': False}])
    with pytest.raises(ValueError, match='advance'):
        pub.guard(event())


def test_alpha_after_stable_release_is_rejected(release_gate, monkeypatch):
    monkeypatch.setattr(pub, 'pages', lambda path: release_gate['reviews'] if path.endswith('/reviews')
                        else [{'tag_name': 'v1.0.0', 'draft': False}])
    with pytest.raises(ValueError, match='advance|alpha'):
        pub.guard(event())


def test_stable_manifest_tag_must_match_version(tmp_path):
    (tmp_path / 'release-manifest.json').write_text(json.dumps(
        {'commit': COMMIT, 'tag': 'v1.0.0', 'python_version': '0.1.0a9', 'assets': {}}))
    with pytest.raises(ValueError, match='version'):
        pub.validate_assets(tmp_path, COMMIT, 'v1.0.0')


def test_stable_publication_marks_latest_without_prerelease(tmp_path, monkeypatch):
    calls = []
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'CHANGELOG.md').write_text('# Changelog\n\n## 1.0.0 — 2026-09-15\nNotes')
    monkeypatch.setattr(pub, 'pages', lambda _: [])
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir()
    (artifacts / 'llm').write_text('x')
    monkeypatch.setattr(pub, 'build', lambda evidence, root: artifacts)
    monkeypatch.setattr(pub, 'validate_assets', lambda *a: None)

    def run(*args, **kwargs):
        calls.append(args)
        if args[:3] == ('git', 'ls-remote', 'origin'):
            return ''
        if args[:2] == ('gh', 'release') and args[2] == 'download':
            (tmp_path / 'downloaded' / 'llm').write_text('x')
        return ''

    monkeypatch.setattr(pub, 'run', run)
    pub.publish({'tag': 'v1.0.0', 'commit': COMMIT}, tmp_path)
    create = next(c for c in calls if c[:3] == ('gh', 'release', 'create'))
    assert '--prerelease' not in create
    assert ('gh', 'release', 'edit', 'v1.0.0', '--repo', pub.REPO, '--draft=false', '--latest=true') in calls
