# Generated-By: Codex / gpt-6-astra
"""Human event view keeps raw evidence and makes export/copy explicit."""
import asyncio
import copy
import json
from unittest.mock import patch

import pytest
pytest.importorskip('textual')
from textual.widgets import Button, Input, RichLog, Static, TextArea
from test_tui import make_app, snapshot
from test_tui_events import BufferedEvents


def source_event(number, kind, **detail):
    return {'id': number, 'timestamp': 1800000000 + number, 'kind': 'data_plane_' + kind,
            'model': 'demo' if kind == 'state' else None,
            'detail': {'source': 'llama-swap', 'received_at': 1800000000 + number,
                       'trusted_for_quiet': False, **detail}}


def test_reconnect_snapshots_are_compact_but_errors_and_raw_records_remain(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        events = BufferedEvents()
        app.event_reader = events
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            records = []
            for _ in range(10):
                for kind, detail in [('error', {'reason': 'timeout'}), ('connection', {'status': 'disconnected'}),
                                     ('connection', {'status': 'connected'}), ('inflight', {'count': None}),
                                     ('state', {'state': 'ready'})]:
                    records.append(source_event(len(records)+1, kind, **detail))
            events.events = copy.deepcopy(records)
            app.update_events()
            await pilot.pause()
            assert app.event_history == records
            log = app.query_one('#events', RichLog)
            visible = ' '.join(line.text for line in log.lines)
            assert '{' not in visible and 'trusted_for_quiet' not in visible
            assert visible.count('timeout') == 1 and visible.count('ready') == 1
            assert '[data-plane]' in visible
            status = str(app.query_one('#source-status', Static).render())
            assert 'connected' in status and 'in-flight: ?' in status and 'errors: 10' in status
            raw = app.event_export_text()
            assert '"trusted_for_quiet": false' in raw and '"count": null' in raw
            assert 'upstream loss unknown' in raw and 'not a daemon stop' in raw
            events.events = [source_event(51, 'state', state='stopped'), source_event(52, 'error', reason='read_failed')]
            app.update_events()
            await pilot.pause()
            visible = ' '.join(line.text for line in log.lines)
            assert 'stopped' in visible and 'read_failed' in visible
            assert len(app.event_history) == 52
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(100, 30), (40, 24)])
def test_details_copy_is_explicit_and_save_is_portable_without_overwrite(snapshot, tmp_path, size):
    async def scenario():
        app, _ = make_app(snapshot)
        events = BufferedEvents()
        app.event_reader = events
        copied = []
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            events.events = [source_event(1, 'error', reason='timeout')]
            app.update_events()
            frozen = app.event_export_text()
            with patch.object(app, 'copy_to_clipboard', side_effect=copied.append):
                await pilot.press('e')
                await pilot.pause()
                area = app.screen.query_one('#event-text', TextArea)
                assert area.read_only and area.text == frozen and copied == []
                area.focus()
                await pilot.press('home', 'shift+right')
                assert area.selected_text
                selected = area.selected_text
                await pilot.click('#event-copy')
                assert copied == [selected]
                result = str(app.screen.query_one('#export-status', Static).render())
                assert result == 'Copy requested. If your terminal blocks clipboard access, use Save text.'
                target = tmp_path / 'events.txt'
                app.screen.query_one('#export-path', Input).value = str(target)
                assert not target.exists()
                await pilot.click('#event-save')
                await app.workers.wait_for_complete()
                assert target.read_text() == frozen and target.stat().st_mode & 0o777 == 0o600
                target.write_text('existing file')
                # Public activation tests overwrite refusal independently of the click animation.
                app.screen.query_one('#event-save', Button).press()
                await pilot.pause()
                await app.workers.wait_for_complete()
                assert target.read_text() == 'existing file'
                assert 'not overwritten' in str(app.screen.query_one('#export-status', Static).render())
                events.events = [source_event(2, 'state', state='ready')]
                app.update_events()
                assert area.text == frozen  # A moving stream cannot change the text being copied.
                await pilot.press('escape')
                assert app.screen is app.dashboard
    asyncio.run(scenario())


def test_normal_copy_uses_compact_summary_for_200_raw_records(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        events = BufferedEvents()
        app.event_reader = events
        copied = []
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            records = [source_event(number, 'state', state='ready') for number in range(1, 201)]
            for number, item in enumerate(records):
                item['model'] = 'synthetic-model-%02d' % (number % 20)
            events.events = copy.deepcopy(records)
            app.update_events()
            raw = app.event_export_text()
            summary = app.event_summary_text()
            assert len(raw.encode('utf-8')) > 65536
            assert len(summary.encode('utf-8')) < 65536
            with patch.object(app, 'copy_to_clipboard', side_effect=copied.append):
                await pilot.press('e')
                await pilot.pause()
                assert app.screen.query_one('#event-text', TextArea).selected_text == ''
                assert copied == []
                await pilot.click('#event-copy')
                assert copied == [summary]
                assert 'Raw JSON:' not in copied[0] and '[data-plane]' in copied[0]
                assert json.loads(raw.split('Raw JSON:\n', 1)[1])['events'] == records
                assert app.screen.text == raw
                app.screen.query_one('#event-text', TextArea).select_all()
                app.screen.query_one('#event-copy', Button).press()
                await pilot.pause()
                assert copied == [summary]  # Oversized explicit selection is not silently truncated.
                assert '64 KiB' in str(app.screen.query_one('#export-status', Static).render())
                await pilot.press('escape')
    asyncio.run(scenario())


def test_copy_fallback_and_cancel_do_not_write_files_or_clipboard(snapshot, tmp_path):
    async def scenario():
        app, _ = make_app(snapshot)
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            with patch.object(app, 'copy_to_clipboard', None):
                await pilot.press('e')
                await pilot.pause()
                target = tmp_path / 'cancelled.txt'
                app.screen.query_one('#export-path', Input).value = str(target)
                await pilot.click('#event-copy')
                assert 'Clipboard unavailable' in str(app.screen.query_one('#export-status', Static).render())
                await pilot.press('escape')
                assert not target.exists()
            with patch.object(app, 'copy_to_clipboard', side_effect=OSError('private backend message')):
                await pilot.press('e')
                await pilot.pause()
                await pilot.click('#event-copy')
                result = str(app.screen.query_one('#export-status', Static).render())
                assert 'failed' in result and 'private backend' not in result
                await pilot.press('escape')
    asyncio.run(scenario())


def test_local_drop_counters_and_inflight_unknown_survive_coalescing_and_reset(snapshot):
    async def scenario():
        app, _ = make_app(snapshot)
        events = BufferedEvents()
        app.event_reader = events
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            records = [source_event(i, 'dropped', dropped=3,
                                   dropped_by_reason={'unlisted_model': 2, 'buffer_full': 1},
                                   upstream_loss_unknown=True) for i in range(1, 4)]
            records += [source_event(4, 'inflight', count=2), source_event(5, 'inflight', count=None)]
            events.events = records
            app.update_events()
            await pilot.pause()
            status = str(app.query_one('#source-status', Static).render())
            assert 'in-flight: ?' in status and 'drops: 9' in status
            data = json.loads(app.event_export_text().split('Raw JSON:\n', 1)[1])
            assert data['source_counters']['local_dropped_by_reason'] == {'unlisted_model': 6, 'buffer_full': 3}
            assert data['source_counters']['upstream_loss_unknown'] is True
            assert data['delivery']['dropped'] == 3  # Client queue loss remains separate.
            late = source_event(6, 'error', reason='timeout')
            late['timestamp'] = 1700000000
            events.events = [late]
            app.update_events()
            assert app.event_presentation.counters()['local_dropped_total'] == 9
            assert app.event_presentation.counters()['errors_by_reason'] == {'timeout': 1}
            visible = ' '.join(' '.join(line.text for line in app.query_one('#events', RichLog).lines).split())
            assert visible.count('local discard') == 1 and 'filtered' in visible and 'overflow' in visible
            app.action_reset_events()
            assert not app.event_history
            assert 'unavailable' in str(app.query_one('#source-status', Static).render())
    asyncio.run(scenario())


def test_saving_can_finish_after_details_close_without_touching_removed_widgets(snapshot, tmp_path):
    async def scenario():
        import threading
        app, _ = make_app(snapshot)
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press('e')
            await pilot.pause()
            dialog = app.screen
            writer = dialog.write_text
            def held(path, text):
                started.set()
                try:
                    assert release.wait(3)
                    writer(path, text)
                finally:
                    finished.set()
            target = tmp_path / 'explicit-save.txt'
            dialog.query_one('#export-path', Input).value = str(target)
            with patch.object(dialog, 'write_text', side_effect=held):
                try:
                    await pilot.click('#event-save')
                    assert await asyncio.to_thread(started.wait, 1)
                    await pilot.press('escape')
                finally:
                    release.set()
                assert await asyncio.to_thread(finished.wait, 2)
                await app.workers.wait_for_complete()
                assert target.read_text() == dialog.text
                assert app._exception is None
    asyncio.run(scenario())


def test_real_textual_copy_emits_osc52_only_after_user_activation(snapshot):
    async def scenario():
        import base64
        app, _ = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            packets = []
            with patch.object(app._driver, 'write', side_effect=packets.append):
                await pilot.press('e')
                await pilot.pause()
                assert not any(packet.startswith('\x1b]52;') for packet in packets)
                expected = app.event_summary_text()
                await pilot.click('#event-copy')
                copies = [packet for packet in packets if packet.startswith('\x1b]52;')]
                assert len(copies) == 1
                assert base64.b64decode(copies[0].split(';', 2)[2].removesuffix('\a')).decode() == expected
                await pilot.press('escape')
    asyncio.run(scenario())


def test_export_refuses_an_existing_symlink(tmp_path):
    from tui.event_view import EventDetails
    original = tmp_path / 'original.txt'
    original.write_text('keep')
    link = tmp_path / 'link.txt'
    link.symlink_to(original)
    with pytest.raises(FileExistsError):
        EventDetails.write_text(str(link), 'replacement')
    assert original.read_text() == 'keep' and link.is_symlink()
