# Generated-By: Codex / gpt-6-astra
# Generated-By: Claude Code / claude-fable-5-1
# Generated-By: OpenCode / deepseek-v4.1-flash
"""Activity availability and source attribution are independent UI observations.

The former model-name/detail line below the model list is intentionally gone
(#250): readable counts stay in the table while failure reasons stay in the
footer and in ``status --json``.
"""
import asyncio
import copy
import json
from unittest.mock import patch

import pytest
pytest.importorskip('textual')
from textual.widgets import DataTable, Static
from test_tui import make_app, snapshot


@pytest.mark.parametrize('sources', [['unknown'], [], ['lab', 'unknown']])
def test_successful_counts_are_kept_when_source_is_unavailable(snapshot, sources):
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
            # The removed detail line leaves no widget and no stale hint behind.
            assert not app.query('#details')
            assert 'Updated' in str(app.query_one('#event-status', Static).render())
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
            assert table.get_cell('research-model', 'USED').plain == '?'
            result = str(app.query_one('#event-status', Static).render())
            # The failure reason stays a real diagnostic in the footer.
            assert 'Partial update' in result and 'activity unavailable' in result
            assert reason in result
            assert 'Updated' not in result and 'SECRET' not in result
            assert 'ValueError' not in result
            assert app.snapshot == failed  # Keep raw structured diagnostics intact.
            args = app.api.build_parser().parse_args(['status', '--json'])
            await app.refresh_state(args).wait()
            assert json.loads(str(app.query_one('#result', Static).render())) == failed
            client.snapshot = snapshot
            await app.refresh_state().wait()
            assert table.get_cell('research-model', '10m').plain == '12'
    asyncio.run(scenario())
