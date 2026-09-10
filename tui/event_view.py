# Generated-By: Codex / gpt-6-astra
"""Compact event presentation and explicit, portable detail export."""
import asyncio
from collections import Counter, OrderedDict
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from rich.text import Text
from textual import work
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TextArea


class EventPresentation:
    """Raw history lives in the app; this bounded projection never edits events."""
    def __init__(self, clean):
        self.clean = clean
        self.reset()

    def reset(self):
        self.seen = False
        self.connection = 'unavailable'
        self.inflight = None
        self.errors = Counter()
        self.drops = Counter()
        self.drop_total = 0
        self.drop_known = True
        self._latest = {}
        self.latest_error = None
        self.reset_display()

    def reset_display(self):
        self.states = OrderedDict()
        self.last_error = None
        self.last_drop_reasons = None
        self.native_errors = None

    @staticmethod
    def source(item):
        detail = item.get('detail')
        return (item['kind'].startswith('data_plane_') and isinstance(detail, dict)
                and detail.get('source') == 'llama-swap')

    @staticmethod
    def reason(detail):
        reason = detail.get('reason')
        return reason if reason in ('timeout', 'disconnected', 'invalid_event', 'limit_exceeded', 'read_failed') else 'unknown'

    def account(self, item):
        if not self.source(item):
            return
        self.seen = True
        kind, detail = item['kind'], item['detail']
        key = (item['timestamp'], item['id'])
        if kind in ('data_plane_connection', 'data_plane_inflight') and key >= self._latest.get(kind, (-float('inf'), -1)):
            self._latest[kind] = key
            if kind == 'data_plane_connection':
                value = detail.get('status')
                self.connection = value if value in ('connecting', 'connected', 'disconnected', 'closed') else 'unknown'
            else:
                value = detail.get('count')
                self.inflight = value if type(value) is int and value >= 0 else None
        if kind == 'data_plane_error':
            self.errors[self.reason(detail)] += 1
            if key >= self._latest.get(kind, (-float('inf'), -1)):
                self._latest[kind] = key
                self.latest_error = self.reason(detail)
        elif kind == 'data_plane_dropped':
            count = detail.get('dropped')
            if type(count) is int and count >= 0:
                self.drop_total += count
            else:
                self.drop_known = False
            reasons = detail.get('dropped_by_reason')
            if isinstance(reasons, dict):
                for reason, count in reasons.items():
                    if type(count) is int and count >= 0:
                        name = reason if reason in ('unlisted_model', 'invalid_event', 'buffer_full', 'limit_exceeded') else 'other'
                        self.drops[name] += count

    def status(self):
        if not self.seen:
            return 'Data-plane: unavailable\nNo source observations received'
        return 'Data-plane: %s · in-flight: %s\nerrors: %s %s · drops: %s' % (
            self.connection, '?' if self.inflight is None else self.inflight,
            sum(self.errors.values()), self.latest_error or '', self.drop_total if self.drop_known else '?')

    def counters(self):
        return {'scope': 'received since local cursor reset; not upstream lifetime totals',
                'errors_by_reason': dict(self.errors), 'local_dropped_by_reason': dict(self.drops),
                'local_dropped_total': self.drop_total if self.drop_known else None,
                'upstream_loss_unknown': True}

    def short(self, value, limit=64):
        text = self.clean(str(value)).replace('\n', ' ')
        return text if len(text) <= limit else text[:limit] + '…'

    def line(self, item):
        kind = item['kind']
        detail = item.get('detail') if isinstance(item.get('detail'), dict) else {}
        relayed = self.source(item)
        model = self.short(item.get('model') or '', 48)
        color, body = 'white', self.short(kind)
        if relayed:
            if kind in ('data_plane_connection', 'data_plane_inflight'):
                return None
            if kind == 'data_plane_state':
                state = self.short(detail.get('state') or 'unknown')
                name = item.get('model')
                if name in self.states and self.states[name] == state:
                    self.states.move_to_end(name)
                    return None
                self.states[name] = state
                self.states.move_to_end(name)
                if len(self.states) > 1024:
                    self.states.popitem(last=False)
                body, color = model + ': observed ' + state, 'blue'
            elif kind == 'data_plane_error':
                reason = self.reason(detail)
                if reason == self.last_error:
                    return None  # Every recurrence still increments the stable error counter.
                self.last_error = reason
                body, color = 'error: ' + reason, 'bright_red'
            elif kind == 'data_plane_dropped':
                reasons = detail.get('dropped_by_reason')
                signature = tuple(sorted(reasons)) if isinstance(reasons, dict) else ()
                if signature == self.last_drop_reasons:
                    return None
                self.last_drop_reasons = signature
                labels = {'unlisted_model': 'filtered', 'invalid_event': 'invalid',
                          'buffer_full': 'overflow', 'limit_exceeded': 'source bound'}
                body = 'local discard: ' + (', '.join(labels.get(key, 'other') for key in signature) or 'unknown')
                body += ' · upstream loss ?'
                color = 'yellow'
            else:
                body = self.short(kind.removeprefix('data_plane_')) + (' ' + model if model else '')
        elif kind == 'state':
            errors = detail.get('errors', [])
            signature = json.dumps(errors, sort_keys=True)
            previous = self.native_errors
            self.native_errors = signature
            if signature == previous or (not errors and previous is None):
                return None
            body = 'observations incomplete (details)' if errors else 'observations available'
            color = 'yellow' if errors else 'white'
        else:
            if model:
                body += ' ' + model
            if detail.get('status') is not None:
                body += ' · ' + self.short(detail['status'])
            if 'error' in kind or 'fail' in kind:
                color = 'bright_red'
            elif 'sleep' in kind:
                color = 'yellow'
            elif 'pin' in kind or 'reserve' in kind:
                color = 'cyan'
            elif 'wake' in kind or 'load' in kind:
                color = 'green'
        try:
            stamp = datetime.fromtimestamp(item['timestamp'], timezone.utc).strftime('%H:%M:%S')
        except (ValueError, OverflowError, OSError):
            stamp = '?'
        return Text('%s [%s] #%s %s' % (stamp, 'data-plane' if relayed else 'scheduler', item['id'], body), style=color)


class EventDetails(ModalScreen):
    """Frozen plain text; no clipboard or filesystem side effect on opening."""
    DEFAULT_CSS = '''
    EventDetails { align: center middle; }
    #event-dialog { width: 96%; height: 94%; background: #202020; padding: 0 1; }
    #event-dialog-title { height: 1; color: #ad8c63; }
    #event-text { height: 1fr; }
    #export-label { height: 1; }
    #export-path { height: 3; }
    #export-buttons { height: 3; }
    #export-buttons Button { width: 1fr; min-width: 0; }
    #export-status { height: 3; color: #b0b0b0; }
    '''
    BINDINGS = [('escape', 'close', 'Close')]
    COPY_LIMIT = 65536

    def __init__(self, text, summary):
        super().__init__()
        self.text = text
        self.summary = summary
        self.saving = False
        self._status_widget = None
        self._owner_app = None

    def compose(self):
        with Vertical(id='event-dialog'):
            yield Static('Event details · frozen snapshot', id='event-dialog-title', markup=False)
            yield TextArea(self.text, read_only=True, soft_wrap=True, id='event-text')
            yield Static('Save UTF-8 text on this machine:', id='export-label')
            yield Input(placeholder='Choose a new file path', id='export-path')
            with Horizontal(id='export-buttons'):
                yield Button('Copy', id='event-copy')
                yield Button('Save text', id='event-save')
                yield Button('Close', id='event-close')
            yield Static('Shift+arrows select. Copy uses selection or compact summary. Save text keeps full raw data.',
                         id='export-status', markup=False)

    def on_mount(self):
        self._owner_app = self.app
        self._status_widget = self.query_one('#export-status', Static)
        self.query_one('#event-text', TextArea).focus()

    def say(self, message):
        if (self._owner_app is not None and self._owner_app.is_running
                and self.is_attached and self._status_widget.is_attached):
            self._status_widget.update(message)

    def on_input_submitted(self, event):
        event.stop()  # A filename must never bubble into the scheduler command parser.
        self.say('Use Save text to save this path.')

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'event-copy':
            text = self.query_one('#event-text', TextArea).selected_text or self.summary
            copier = getattr(self.app, 'copy_to_clipboard', None)
            if not callable(copier) or getattr(self.app, '_driver', None) is None:
                self.say('Clipboard unavailable here; view or Save text instead.')
            elif len(text.encode('utf-8')) > self.COPY_LIMIT:
                self.say('Clipboard request exceeds 64 KiB; select less text or Save text.')
            else:
                try:
                    copier(text)
                except Exception:
                    self.say('Clipboard request failed; view or Save text instead.')
                else:
                    self.say('Copy requested. If your terminal blocks clipboard access, use Save text.')
        elif event.button.id == 'event-save':
            path = self.query_one('#export-path', Input).value.strip()
            if path:
                self.save_text(path)
            else:
                self.say('Choose a new file path, then press Save text. Nothing written.')
        elif event.button.id == 'event-close':
            self.action_close()

    @staticmethod
    def write_text(path, text):
        # Exclusive creation refuses existing files and symlinks; private permissions.
        fd = os.open(Path(path).expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)

    @work
    async def save_text(self, path):
        if self.saving:
            return
        self.saving = True
        try:
            await asyncio.to_thread(self.write_text, path, self.text)
        except FileExistsError:
            self.say('Existing file not overwritten; choose a new path.')
        except (OSError, ValueError):
            self.say('Save failed; output may be incomplete. Check path and permissions.')
        else:
            self.say('Saved UTF-8 text on this machine: ' + path)
        finally:
            self.saving = False

    def action_close(self):
        self.dismiss()
