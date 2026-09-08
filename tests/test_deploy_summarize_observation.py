# Generated-By: Codex / gpt-6-astra
import copy
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess

import pytest

from deploy import summarize_observation as summary

START = datetime(2026, 9, 8, tzinfo=timezone.utc).timestamp()
URL = 'http://127.0.0.1:8011/v1/state'


def artifact(directory, number, seconds, *, external=10, state_seconds=None, status='ok', extra=None, source=True):
    directory.mkdir(exist_ok=True)
    captured = START + seconds
    source_record = {'name': 'scheduler-state', 'type': 'http_json', 'status': status,
                     'url': URL, 'http_status': 200, 'started_at': summary.iso(captured + .01),
                     'ended_at': summary.iso(captured + .02),
                     'json': {'schema_version': 1, 'sampled_at': START + (seconds if state_seconds is None else state_seconds),
                              'read_only': True, 'errors': [],
                              'gpus': [{'index': 0, 'uuid': 'GPU-zero', 'total_gb': 100, 'external_gb': external}]}}
    if extra:
        source_record.update(extra)
    snapshot = {'schema_version': 1, 'sampled_at': summary.iso(captured), 'read_only': True, 'dry_run': False,
                'provenance': {'config_path': '/private/capture.json',
                               'sources': [{'name': 'scheduler-state', 'type': 'http_json', 'url': URL}]},
                'sources': [source_record] if source else []}
    snap_path = directory / ('snapshot-%04d.json' % number)
    manifest_path = directory / ('manifest-%04d.json' % number)
    def save(data=snapshot):
        raw = (json.dumps(data, sort_keys=True) + '\n').encode()
        snap_path.write_bytes(raw)
        manifest = {'schema_version': 1, 'snapshot': snap_path.name, 'sha256': hashlib.sha256(raw).hexdigest(),
                    'created_at': summary.iso(captured + .03), 'provenance': data['provenance']}
        manifest_path.write_text(json.dumps(manifest))
    save()
    return snapshot, snap_path, manifest_path, save


def test_exact_counts_timestamps_and_external_distribution(tmp_path):
    for index, value in enumerate([0, 20, 40]):
        artifact(tmp_path, index, index * 15, external=value)
    report = summary.summarize(tmp_path)
    assert report['sample_count'] == 3
    assert report['sources']['scheduler-state'] == {'observations': 3, 'valid': 3, 'failures': 0, 'truncated': 0, 'missing': 0}
    assert report['capture_coverage']['first_sampled_at'] == summary.iso(START)
    assert report['state_coverage']['observed_span_seconds'] == 30
    assert report['state_coverage']['sampled_window_complete']
    gpu = report['gpus'][0]
    assert gpu['identity'] == 'uuid:GPU-zero' and gpu['indices_seen'] == [0]
    stats = gpu['reported_external_gib']
    assert {key: stats[key] for key in ['count', 'mean', 'p50', 'p95', 'p99', 'zero_count', 'positive_count']} == {
        'count': 3, 'mean': 20, 'p50': 20, 'p95': 38, 'p99': 39.6, 'zero_count': 1, 'positive_count': 2}
    assert stats['histogram'] == [{'lower_inclusive': x, 'upper_exclusive': x + 10, 'count': 1} for x in [0, 20, 40]]
    assert report['scope']['long_term_stability'] == 'NOT MEASURED'
    assert not report['scope']['calendar_wait_required']
    assert not report['scope']['continuous_running_proven']


def test_duplicates_and_ordering_do_not_inflate_samples(tmp_path):
    artifact(tmp_path, 0, 30, external=30)
    _, snap, manifest, _ = artifact(tmp_path, 1, 0, external=0)
    (tmp_path / 'manifest-0002.json').write_bytes(manifest.read_bytes())
    copied = tmp_path / 'snapshot-copy.json'
    copied.write_bytes(snap.read_bytes())
    duplicate = json.loads(manifest.read_text())
    duplicate['snapshot'] = copied.name
    (tmp_path / 'manifest-0003.json').write_text(json.dumps(duplicate))
    report = summary.summarize(tmp_path)
    assert report['integrity']['sha_verified_records'] == 4
    assert report['integrity']['duplicate_snapshot_references'] == 1
    assert report['integrity']['duplicate_snapshot_payloads'] == 1
    assert report['integrity']['capture_order_inversions'] == 1
    assert report['state_observations']['order_inversions'] == 1
    assert report['sample_count'] == 2
    assert report['gpus'][0]['reported_external_gib']['count'] == 2


def test_requested_window_reports_missing_edges_and_internal_intervals(tmp_path):
    for index, seconds in enumerate([0, 15, 60]):
        artifact(tmp_path, index, seconds)
    report = summary.summarize(tmp_path, window_start=summary.iso(START), window_end=summary.iso(START + 90))
    coverage = report['state_coverage']
    assert coverage['estimated_missing_intervals'] == 4
    assert coverage['expected_samples_at_configured_cadence'] == 7
    assert coverage['max_internal_gap_seconds'] == 45
    assert coverage['trailing_gap_seconds'] == 30
    assert not coverage['sampled_window_complete']


def test_long_gap_never_becomes_continuity_or_calibration(tmp_path):
    artifact(tmp_path, 0, 0)
    artifact(tmp_path, 1, 604800)
    report = summary.summarize(tmp_path)
    assert report['capture_coverage']['observed_span_seconds'] == 604800
    assert not report['state_coverage']['sampled_window_complete']
    assert report['state_coverage']['estimated_missing_intervals'] == 40319
    assert report['scope']['threshold_calibration'] == 'NOT MEASURED'
    assert not report['scope']['continuous_running_proven']


def test_bad_files_hashes_and_missing_manifests_remain_explicit(tmp_path):
    artifact(tmp_path, 0, 0)
    _, missing, _, _ = artifact(tmp_path, 1, 15)
    missing.unlink()
    _, _, wrong, _ = artifact(tmp_path, 2, 30)
    data = json.loads(wrong.read_text()); data['sha256'] = '0' * 64; wrong.write_text(json.dumps(data))
    _, _, orphan_manifest, _ = artifact(tmp_path, 3, 45)
    orphan_manifest.unlink()
    (tmp_path / 'manifest-corrupt.json').write_text('{')
    report = summary.summarize(tmp_path)
    assert {row['reason'] for row in report['integrity']['rejected']} == {
        'missing_file', 'snapshot_hash_mismatch', 'unreadable_or_invalid_json'}
    assert 'snapshot-0003.json' in report['integrity']['unreferenced_snapshots']
    assert report['sample_count'] == 1


def test_source_errors_truncation_staleness_and_missing_sources(tmp_path):
    artifact(tmp_path, 0, 0)
    artifact(tmp_path, 1, 15, status='unavailable', extra={'error': 'url_unavailable', 'detail': 'PRIVATE_SECRET'})
    artifact(tmp_path, 2, 30, extra={'truncated': {'stdout': True, 'stderr': False}})
    artifact(tmp_path, 3, 45, state_seconds=-60)
    artifact(tmp_path, 4, 60, source=False)
    report = summary.summarize(tmp_path)
    assert report['sources']['scheduler-state'] == {'observations': 4, 'valid': 2, 'failures': 2, 'truncated': 1, 'missing': 1}
    assert report['state_observations']['errors'] == {'stale_or_future_state': 1}
    assert report['state_coverage']['unique_samples'] == 1
    assert not report['state_coverage']['sampled_window_complete']
    assert 'PRIVATE_SECRET' not in summary.render(report, 'json')


def test_repeated_cached_state_does_not_manufacture_coverage(tmp_path):
    for index in range(3):
        artifact(tmp_path, index, index * 15, state_seconds=0)
    report = summary.summarize(tmp_path, max_state_age_seconds=60)
    assert report['capture_coverage']['sampled_window_complete']
    assert report['state_observations']['duplicate_timestamps'] == 2
    assert report['gpus'][0]['reported_external_gib']['count'] == 1
    assert not report['state_coverage']['sampled_window_complete']
    assert report['state_coverage']['trailing_gap_seconds'] == 30


def test_conflicting_capture_or_state_timestamps_are_excluded(tmp_path):
    capture_dir = tmp_path / 'capture'
    artifact(capture_dir, 0, 0, external=10)
    artifact(capture_dir, 1, 0, external=20)
    result = summary.summarize(capture_dir)
    assert result['integrity']['conflicting_capture_records'] == 2 and result['sample_count'] == 0
    state_dir = tmp_path / 'states'
    artifact(state_dir, 0, 0, state_seconds=0, external=10)
    artifact(state_dir, 1, 15, state_seconds=0, external=20)
    result = summary.summarize(state_dir)
    assert result['state_observations']['conflicting_records'] == 2 and result['gpus'] == []


def test_missing_unknown_gpu_values_and_uuid_changes_are_not_zero_filled(tmp_path):
    artifact(tmp_path, 0, 0, external=0)
    artifact(tmp_path, 1, 15, external=None)
    payload, _, _, save = artifact(tmp_path, 2, 30, external=90)
    payload['sources'][0]['json']['gpus'][0]['uuid'] = 'GPU-replacement'
    save()
    report = summary.summarize(tmp_path)
    assert len(report['gpus']) == 2
    original = next(row for row in report['gpus'] if row['identity'] == 'uuid:GPU-zero')
    assert original['unknown_external_samples'] == 1
    assert original['missing_from_state_samples'] == 1
    assert original['reported_external_gib']['count'] == 1 and original['reported_external_gib']['mean'] == 0


@pytest.mark.parametrize('value', [-1, True, 101, 10**400])
def test_invalid_external_values_are_unknown_not_occupancy(tmp_path, value):
    artifact(tmp_path, 0, 0, external=value)
    stats = summary.summarize(tmp_path)['gpus'][0]
    assert stats['unknown_external_samples'] == 1 and stats['reported_external_gib']['mean'] is None


def test_mixed_source_identities_are_not_pooled(tmp_path):
    artifact(tmp_path, 0, 0)
    payload, _, _, save = artifact(tmp_path, 1, 15)
    other = 'http://127.0.0.1:9999/v1/state'
    payload['sources'][0]['url'] = other
    payload['provenance']['sources'][0]['url'] = other
    save()
    result = summary.summarize(tmp_path)
    assert result['state_observations']['errors']['mixed_state_source_identities'] == 2
    assert len(result['state_observations']['source_identity_sha256']) == 2
    assert result['gpus'] == [] and not result['state_coverage']['sampled_window_complete']


def test_unverified_source_provenance_is_blocked(tmp_path):
    payload, _, _, save = artifact(tmp_path, 0, 0)
    payload['sources'][0]['url'] = 'http://127.0.0.1:9999/v1/state'
    save()
    assert summary.summarize(tmp_path)['state_observations']['errors'] == {'unverified_state_source_identity': 1}


def test_symlinks_and_traversal_never_read_foreign_artifacts(tmp_path):
    data = tmp_path / 'input'; data.mkdir()
    secret = tmp_path / 'secret'; secret.write_text('PRIVATE_SECRET')
    (data / 'snapshot-link.json').symlink_to(secret)
    (data / 'manifest-link.json').write_text(json.dumps({'snapshot': 'snapshot-link.json'}))
    (data / 'manifest-traversal.json').write_text(json.dumps({'snapshot': '../secret'}))
    (data / 'manifest-symlink.json').symlink_to(secret)
    report = summary.summarize(data)
    assert len(report['integrity']['rejected']) == 3
    assert 'PRIVATE_SECRET' not in summary.render(report, 'json')


def test_reproducible_json_and_lossless_long_csv(tmp_path):
    artifact(tmp_path, 0, 0)
    first = summary.render(summary.summarize(tmp_path), 'json')
    os.utime(tmp_path / 'manifest-0000.json', (1, 1))
    assert summary.render(summary.summarize(tmp_path), 'json') == first
    data = list(csv.DictReader(io.StringIO(summary.render(json.loads(first), 'csv'))))
    leaves = {row['field']: json.loads(row['json_value']) for row in data}
    assert leaves['/sample_count'] == 1
    assert leaves['/gpus/0/identity'] == 'uuid:GPU-zero'
    assert leaves['/scope/long_term_stability'] == 'NOT MEASURED'


def test_cli_offline_dry_run_and_private_output_without_overwrite(tmp_path, monkeypatch, capsys):
    input_dir = tmp_path / 'input'
    artifact(input_dir, 0, 0)
    def forbidden(*args, **kwargs): pytest.fail('offline summary attempted process/network I/O')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    output = tmp_path / 'summary.json'
    before = {p.name: p.read_bytes() for p in input_dir.iterdir()}
    args = ['--input-dir', str(input_dir), '--output', str(output)]
    assert summary.main(args + ['--dry-run']) == 0
    assert not output.exists() and json.loads(capsys.readouterr().out)['sample_count'] == 1
    assert summary.main(args) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    assert summary.main(args) == 2 and output.read_bytes() == original
    assert summary.main(['--input-dir', str(input_dir), '--output', str(input_dir / 'summary.json')]) == 2
    assert before == {p.name: p.read_bytes() for p in input_dir.iterdir()}


def test_corruption_duplicate_keys_and_oversize_files_are_reported(tmp_path, monkeypatch):
    _, snapshot, manifest, _ = artifact(tmp_path, 0, 0)
    snapshot.write_text('{"schema_version":1,"schema_version":1}')
    result = summary.summarize(tmp_path)
    assert result['integrity']['rejected'][0]['reason'] == 'duplicate_json_key'
    monkeypatch.setattr(summary, 'MAX_FILE_BYTES', 4)
    assert summary.summarize(tmp_path)['integrity']['rejected'][0]['reason'] == 'file_too_large'


def test_empty_and_explicit_incomplete_window(tmp_path):
    report = summary.summarize(tmp_path, window_start=summary.iso(START), window_end=summary.iso(START + 120))
    assert report['sample_count'] == 0
    assert report['capture_coverage']['estimated_missing_intervals'] == 9
    assert report['gpus'] == [] and not report['state_coverage']['sampled_window_complete']


@pytest.mark.parametrize('options', [dict(interval_seconds=float('nan')), dict(tolerance_seconds=15),
    dict(bucket_gib=0), dict(window_start='naive'), dict(window_start='2026-09-08', window_end='2026-09-09')])
def test_invalid_configuration_fails_before_analysis(tmp_path, options):
    with pytest.raises(summary.SummaryError): summary.summarize(tmp_path, **options)


def test_extreme_finite_numbers_fail_as_structured_range_errors(tmp_path, capsys):
    assert not summary.finite(10**400)
    with pytest.raises(summary.SummaryError, match='numeric_range'):
        summary.distribution([1e308], 1e-308)
    with pytest.raises(summary.SummaryError, match='resolution'):
        summary.distribution([1e308], 10.0)
    assert summary.main(['--input-dir', str(tmp_path), '--interval-seconds', '1e-308', '--tolerance-seconds', '0',
                         '--window-start', summary.iso(START), '--window-end', summary.iso(START + 120)]) == 2
    assert 'cadence_numeric_range' in capsys.readouterr().err


def test_real_capture_writer_format_is_consumed_without_any_probe(tmp_path, monkeypatch):
    from deploy import capture
    payload, _, _, _ = artifact(tmp_path / 'fixture', 0, 0)
    monkeypatch.setattr(capture, 'utc_now', lambda: datetime.fromtimestamp(START + .03, timezone.utc))
    out = tmp_path / 'actual-format'
    capture.write_outputs(out, payload)
    result = summary.summarize(out)
    assert result['sample_count'] == 1 and result['sources']['scheduler-state']['valid'] == 1
    assert not result['integrity']['rejected']
    assert result['gpus'][0]['reported_external_gib']['mean'] == 10


def test_command_validity_and_http_output_limit_are_separate_from_gpu_data(tmp_path):
    payload, _, _, save = artifact(tmp_path, 0, 0)
    payload['sources'].append({'name': 'command', 'type': 'command', 'status': 'ok', 'returncode': 0,
        'started_at': summary.iso(START + .01), 'ended_at': summary.iso(START + .02),
        'stdout': 'DO_NOT_EXPORT', 'truncated': {'stdout': False, 'stderr': False}})
    payload['provenance']['sources'].append({'name': 'command', 'type': 'command', 'argv': ['never-run']})
    save()
    artifact(tmp_path, 1, 15, status='unavailable', extra={'error': 'output_limit_exceeded'})
    result = summary.summarize(tmp_path)
    assert result['sources']['command']['valid'] == 1 and result['sources']['command']['missing'] == 1
    assert result['sources']['scheduler-state']['truncated'] == 1
    assert 'DO_NOT_EXPORT' not in summary.render(result, 'json')


def test_jitter_tolerance_and_window_filtering_are_deterministic(tmp_path):
    for index, seconds in enumerate([-15, 0, 16, 30, 45]):
        artifact(tmp_path, index, seconds)
    result = summary.summarize(tmp_path, window_start=summary.iso(START), window_end=summary.iso(START + 30))
    assert result['sample_count'] == 3 and result['capture_coverage']['sampled_window_complete']
    assert result['capture_coverage']['estimated_missing_intervals'] == 0
    assert result['gpus'][0]['reported_external_gib']['count'] == 3


def test_same_source_name_with_changed_capture_configuration_is_not_pooled(tmp_path):
    artifact(tmp_path, 0, 0)
    payload, _, _, save = artifact(tmp_path, 1, 15)
    payload['provenance']['config_path'] = '/different/capture.json'
    save()
    report = summary.summarize(tmp_path)
    assert 'mixed_state_source_identities' in report['state_observations']['errors']
    assert not report['gpus']


def test_output_race_preserves_foreign_file_and_cleans_only_own_temp(tmp_path, monkeypatch):
    output = tmp_path / 'summary.json'
    real_link = summary.os.link
    def collide(source, destination):
        output.write_text('foreign')
        return real_link(source, destination)
    monkeypatch.setattr(summary.os, 'link', collide)
    with pytest.raises(FileExistsError):
        summary.write_report(output, 'ours')
    assert output.read_text() == 'foreign'
    assert list(tmp_path.iterdir()) == [output]


def test_missing_input_directory_is_a_command_error(tmp_path, capsys):
    assert summary.main(['--input-dir', str(tmp_path / 'missing')]) == 2
    assert 'input_directory_unavailable' in capsys.readouterr().err


@pytest.mark.parametrize('sampled', [99, 100.015])
def test_fresh_state_outside_inferred_capture_start_bounds_is_kept_for_statistics(tmp_path, sampled):
    artifact(tmp_path, 0, 100, state_seconds=sampled, external=12)
    result = summary.summarize(tmp_path)
    assert result['state_observations']['fresh'] == 1
    assert result['state_observations']['unique_for_statistics'] == 1
    assert result['gpus'][0]['first_sampled_at'] == summary.iso(START + sampled)
    assert result['gpus'][0]['reported_external_gib']['mean'] == 12
    assert not result['state_coverage']['continuous_running_proven']


def test_explicit_window_still_filters_actual_collector_timestamp(tmp_path):
    artifact(tmp_path, 0, 100, state_seconds=99)
    result = summary.summarize(tmp_path, window_start=summary.iso(START + 100), window_end=summary.iso(START + 120))
    assert result['sample_count'] == 1 and result['sources']['scheduler-state']['valid'] == 1
    assert result['state_observations']['unique_for_statistics'] == 0 and result['gpus'] == []


def test_csv_field_paths_preserve_literal_punctuation_and_escaping():
    report = {'sources': {'a.b': {'valid': 1}, 'a/b~c': {'valid': 2}}}
    rows = list(csv.DictReader(io.StringIO(summary.render(report, 'csv'))))
    assert {row['field']: json.loads(row['json_value']) for row in rows} == {
        '/sources/a.b/valid': 1, '/sources/a~1b~0c/valid': 2}
