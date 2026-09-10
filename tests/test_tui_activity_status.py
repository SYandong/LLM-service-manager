# Generated-By: Codex / gpt-6-astra
"""Activity availability and source attribution are independent UI observations."""
import asyncio
import copy
import json
from unittest.mock import patch

import pytest
pytest.importorskip('textual')
from textual.widgets import DataTable, Static
from test_tui import make_app, snapshot


@pytest.mark.parametrize('sources, expected', [
    (['unknown'], 'source unavailable'),
    ([], 'source unavailable'),
    (['lab', 'unknown'], 'from lab · some sources unavailable'),
])
def test_successful_counts_are_kept_when_source_is_unavailable(snapshot, sources, expected):
    async def scenario():
        snapshot['errors'] = []
        snapshot['activity'][1]['by'] = sources
        original = copy.deepcopy(snapshot)
        app, _ = make_app(snapshot)
        async with app.run_test(size=(200, 30)) as pilot:
            await app.workers.wait_for_complete()
            table = app.query_one('#models', DataTable)
            table.move_cursor(row=1, animate=False)
            await pilot.pause()
            assert table.get_cell('research-model', '10m').plain == '12'
            detail = str(app.query_one('#details', Static).render())
            assert expected in detail
            assert 'from unknown' not in detail and 'not recorded' not in detail
            assert 'activity unavailable' not in detail
            assert 'Updated' in str(app.query_one('#result', Static).render())
            assert app.snapshot == original
    asyncio.run(scenario())


@pytest.mark.parametrize('code, reason', [
    ('deadline', 'read budget exceeded'), ('locked', 'database busy'),
    ('schema', 'schema unavailable or unsupported'), ('parse', 'invalid activity data'),
    ('unavailable', 'source unavailable'), ('corrupt', 'database damaged'),
    ('interrupted', 'read interrupted'), ('read_failed', 'read failed'),
    ('io', 'database read I/O failed'),
    ('round_deadline', 'collector round deadline exceeded'),
    ('previous_probe_running', 'previous activity read still running'),
    ('not configured', 'source not configured'),
    ('ValueError', 'reason unavailable'),
    ('unexpected SQL /private/SECRET token', 'reason unavailable'),
])
def test_failed_read_is_partial_and_never_displays_stale_counts(snapshot, code, reason):
    async def scenario():
        snapshot['errors'] = []
        app, client = make_app(snapshot)
        async with app.run_test(size=(200, 30)) as pilot:
            await app.workers.wait_for_complete()
            table = app.query_one('#models', DataTable)
            table.move_cursor(row=1, animate=False)
            await pilot.pause()
            before = table.rows.copy()
            failed = copy.deepcopy(snapshot)
            failed['errors'] = ['activity: ' + code]
            # Even a server response containing old-looking rows must not make
            # an explicitly failed activity read look like fresh counts.
            client.snapshot = failed
            with patch.object(table, 'clear', wraps=table.clear) as clear:
                await app.refresh_state().wait()
                assert clear.call_count == 0
            assert app.selected_model() == 'research-model'
            assert all(table.rows[key] is row for key, row in before.items())
            assert table.get_cell('research-model', '10m').plain == '?'
            assert table.get_cell('research-model', 'FROM').plain == '?'
            result = str(app.query_one('#result', Static).render())
            detail = str(app.query_one('#details', Static).render())
            assert result.startswith('Partial update') and 'activity unavailable' in result
            assert reason in result and reason in detail
            assert 'Updated' not in result and 'SECRET' not in result + detail
            assert 'ValueError' not in result + detail
            assert app.snapshot == failed  # Keep raw structured diagnostics intact.
            args = app.api.build_parser().parse_args(['status', '--json'])
            await app.refresh_state(args).wait()
            assert json.loads(str(app.query_one('#result', Static).render())) == failed
            client.snapshot = snapshot
            await app.refresh_state().wait()
            assert table.get_cell('research-model', '10m').plain == '12'
            assert 'from ctr-b' in str(app.query_one('#details', Static).render())
    asyncio.run(scenario())
