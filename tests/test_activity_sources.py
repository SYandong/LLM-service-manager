# Generated-By: Codex / gpt-6-astra
"""Synthetic source-format compatibility, not live producer attribution proof."""
import json
import sqlite3
from pathlib import Path

import pytest

from llmsvc.activity import ActivityReader


FIXTURE = json.loads(
    (Path(__file__).parent / 'fixtures/telemetry/source-ip-formats.json').read_text()
)


def create_source_db(path, cases):
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE activity (
            id INTEGER PRIMARY KEY, ts_created INTEGER NOT NULL,
            model_id TEXT NOT NULL, src TEXT, metadata_json TEXT,
            input_tokens INTEGER, output_tokens INTEGER
        )''')
        for index, case in enumerate(cases, 1):
            db.execute('INSERT INTO activity VALUES (?, ?, ?, ?, ?, ?, ?)', (
                index, 100, case['name'], case['src'],
                json.dumps(case['metadata']), 2, 3,
            ))


@pytest.mark.parametrize('case', FIXTURE['cases'], ids=lambda case: case['name'])
def test_source_formats_keep_counts_and_map_only_valid_addresses(tmp_path, case):
    path = tmp_path / 'activity.sqlite'
    create_source_db(path, [case])
    reader = ActivityReader(path, case['mapping'])

    activity = reader.read(now=200)[case['name']]
    assert reader.last_error is None
    assert activity['requests_last_hour'] == activity['requests_last_10m'] == 1
    assert activity['source_ip'] == case['source_ip']
    assert activity['source_container'] == case['source_container']

    for by in ('container', 'ip'):
        usage = reader.usage(days=7, by=by, now=200)
        assert usage['known'] is True
        assert usage['totals'] == {'requests': 1, 'input_tokens': 2, 'output_tokens': 3}
        row, = usage['rows']
        expected = case['source_container'] if by == 'container' else case['source_ip'] or 'unknown'
        assert row[by] == expected
        assert row['source_known'] is (case['source_ip'] is not None)
        assert row['source_ips'] == ((case['source_ip'],) if case['source_ip'] else ())


def test_old_unknown_rows_and_equivalent_new_sources_keep_distinct_attribution(tmp_path):
    cases = [case for case in FIXTURE['cases'] if case['name'] in {
        'old_absent', 'old_empty', 'ipv6_compressed', 'ipv6_expanded',
    }]
    path = tmp_path / 'activity.sqlite'
    create_source_db(path, cases)
    reader = ActivityReader(path, {'2001:0DB8:0:0:0:0:0:10': 'fixture-v6'})
    before = path.read_bytes()

    usage = reader.usage(days=30, now=200)
    assert usage['known'] is True
    assert usage['totals'] == {'requests': 4, 'input_tokens': 8, 'output_tokens': 12}
    rows = {row['container']: row for row in usage['rows']}
    assert rows['unknown']['requests'] == rows['fixture-v6']['requests'] == 2
    assert rows['unknown']['source_ips'] == ()
    assert rows['fixture-v6']['source_ips'] == ('2001:db8::10',)
    assert path.read_bytes() == before  # No source backfill or database mutation.


def test_equivalent_mapping_keys_must_not_silently_choose_an_owner(tmp_path):
    with pytest.raises(ValueError, match='conflicting container mappings'):
        ActivityReader(tmp_path / 'unused.sqlite', {
            '192.0.2.10': 'fixture-a', '::ffff:192.0.2.10': 'fixture-b',
        })
