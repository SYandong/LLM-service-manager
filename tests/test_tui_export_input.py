# Generated-By: Codex / gpt-6-astra
"""A terminal may deliver editing controls and text in one input burst."""
import asyncio

import pytest
pytest.importorskip('textual')
from textual import events
import textual.app as textual_app
from textual.widgets import Button, Input
from test_tui import make_app, snapshot


def post_keys(app, keys):
    # Same app input-message entry as the terminal driver, deliberately without
    # a Pilot idle barrier between keys that could hide ordering defects.
    for key in keys:
        char = key if len(key) == 1 else None
        if char is not None and not char.isalnum():
            key = textual_app._character_to_key(char)
        message = events.Key(key, char)
        message.set_sender(app)
        app.post_message(message)


@pytest.mark.parametrize('keys, expected', [
    (['ctrl+a', 'ctrl+k', *'new-file.txt'], 'new-file.txt'),
    ([*'prefix', 'home', 'ctrl+k'], ''),
    (['home', 'ctrl+k', *'用户 events.txt'], '用户 events.txt'),
    (['ctrl+e', 'ctrl+u', *'new-file.txt'], 'new-file.txt'),
    (['end', 'backspace', 'Z'], 'old-file.txZ'),
    (['home', 'right', 'Z'], 'oZld-file.txt'),
])
def test_export_path_edit_keys_keep_order_in_one_batch(snapshot, keys, expected):
    async def scenario():
        app, client = make_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press('e')
            await pilot.pause()
            dialog = app.screen
            field = dialog.query_one('#export-path', Input)
            field.value = 'old-file.txt'
            field.focus()
            await pilot.pause()
            post_keys(app, keys)
            await pilot.pause()
            assert field.value == expected
            assert app.screen is dialog
            assert all(call == ('GET', '/v1/state') for call in client.calls)
            assert not app._write_busy
            await pilot.press('escape')
    asyncio.run(scenario())


def test_batched_replacement_then_save_reaches_the_existing_file_guard(snapshot, tmp_path):
    async def scenario():
        app, client = make_app(snapshot)
        target = tmp_path / 'existing.txt'
        target.write_text('keep')
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press('e')
            await pilot.pause()
            dialog = app.screen
            field = dialog.query_one('#export-path', Input)
            field.value = str(tmp_path / 'old.txt')
            field.focus()
            await pilot.pause()
            post_keys(app, ['ctrl+a', 'ctrl+k', *str(target)])
            await pilot.pause()
            assert field.value == str(target)
            assert target.read_text() == 'keep'  # Typing alone never saves.
            dialog.query_one('#event-save', Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert 'not overwritten' in str(dialog.query_one('#export-status').render())
            assert target.read_text() == 'keep'
            assert all(call == ('GET', '/v1/state') for call in client.calls)
            await pilot.press('escape')
    asyncio.run(scenario())
