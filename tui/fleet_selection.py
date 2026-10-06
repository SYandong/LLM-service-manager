# Generated-By: Codex / gpt-6.1-sol
# Generated-By: Codex / unknown model
"""Mouse selection for the fleet's left-aligned Rich text panels."""

from rich._wrap import divide_line
from rich.cells import cell_len
from rich.text import Text
from textual.message import Message
from textual.widgets import Static


class SelectableStatic(Static, can_focus=True):
    """Keep a selectable source snapshot until the user clears the selection.

    Selection endpoints are caret boundaries in terminal cells. Soft wraps do
    not add newlines to copied text; explicit source newlines are preserved.
    """

    ALLOW_SELECT = False

    class SelectionChanged(Message):
        def __init__(self, selection):
            super().__init__()
            self.selection = selection

        @property
        def control(self):
            return self.selection

    def __init__(self, content="", **kwargs):
        self._source_text = Text()
        self._pending_text = None
        self._anchor = None
        self._endpoint = None
        self._drag_origin = None
        self._dragging = False
        self._capture_app = None
        self._suppress_selection_click = False
        self._selection_closed = False
        self._selection_markup = kwargs.get("markup", True)
        self._selection_lines = []
        self._selection_width = 0
        super().__init__("", **kwargs)
        SelectableStatic.update(self, content)

    @property
    def selected_text(self):
        if not self.has_selection:
            return ""
        start, end = sorted((self._anchor, self._endpoint))
        return self._source_text.plain[start:end]

    @property
    def has_selection(self):
        return self._anchor is not None and self._endpoint != self._anchor

    @property
    def dragging(self):
        return self._dragging

    def update(self, content=""):
        if self._selection_closed:
            return
        text = content.copy() if isinstance(content, Text) else (
            Text.from_markup(str(content)) if self._selection_markup else Text(str(content)))
        if self.dragging or self.has_selection:
            self._pending_text = text
            return
        self._source_text = text
        self._selection_width = 0
        self.clear_cached_dimensions()
        self.refresh(layout=True)

    def render(self):
        text = self._source_text.copy()
        if self.has_selection:
            start, end = sorted((self._anchor, self._endpoint))
            text.stylize("reverse", start, end)
        return text

    def clear_selection(self, *, apply_pending=True):
        changed = self.dragging or self.has_selection
        if self.dragging:
            self._suppress_selection_click = True
            if self._capture_app is not None and self._capture_app.mouse_captured is self:
                self.suppress_click()
        self._dragging = False
        self._anchor = self._endpoint = self._drag_origin = None
        self._release_capture()
        pending = self._pending_text if apply_pending else None
        if apply_pending:
            self._pending_text = None
        if not self._selection_closed:
            if pending is not None:
                self.update(pending)
            else:
                self.refresh()
            if changed:
                self.post_message(self.SelectionChanged(self))

    def consume_selection_click(self, event):
        """Call before a subclass handles clickable allocation targets."""
        if not self._suppress_selection_click:
            return False
        self._suppress_selection_click = False
        event.stop()
        event.prevent_default()
        return True

    def on_click(self, event):
        self.consume_selection_click(event)

    def _release_capture(self):
        app, self._capture_app = self._capture_app, None
        if app is not None and app.mouse_captured is self:
            app.capture_mouse(None)

    def _wrapped_lines(self, width):
        if width == self._selection_width:
            return self._selection_lines
        lines = []
        source_start = 0
        for source_line in self._source_text.plain.split("\n"):
            expanded, boundaries = "", [source_start]
            for index, character in enumerate(source_line):
                if character == "\t":
                    spaces = 8 - cell_len(expanded) % 8
                    expanded += " " * spaces
                    boundaries.extend([source_start + index] * (spaces - 1))
                else:
                    expanded += character
                boundaries.append(source_start + index + 1)
            cuts = [0, *divide_line(expanded, width), len(expanded)]
            for start, end in zip(cuts, cuts[1:]):
                lines.append((expanded[start:end], boundaries[start:end + 1]))
            source_start += len(source_line) + 1
        self._selection_width = width
        self._selection_lines = lines
        return lines

    def _offset_at(self, point):
        width = max(1, self.content_size.width)
        lines = self._wrapped_lines(width)
        if point.y < 0:
            return 0
        if point.y >= len(lines):
            return len(self._source_text.plain)
        line, boundaries = lines[point.y]
        if point.x <= 0:
            return boundaries[0]
        if point.x >= width:
            return boundaries[-1]
        for index in range(len(line)):
            if cell_len(line[:index + 1]) > point.x:
                return boundaries[index]
        return boundaries[-1]

    def on_mouse_down(self, event):
        if event.button != 1 or self._selection_closed or not self.display:
            return
        point = event.get_content_offset(self)
        if point is None:
            return
        # Begin on the frame under the pointer, retaining queued refreshes for
        # the next explicit clear rather than moving text before the anchor.
        self.clear_selection(apply_pending=False)
        self._suppress_selection_click = False
        self._anchor = self._endpoint = self._offset_at(point)
        self._drag_origin = point
        self._dragging = True
        self._capture_app = self.app
        self.capture_mouse()
        self.focus(scroll_visible=False)
        self.post_message(self.SelectionChanged(self))
        event.stop()
        event.prevent_default()

    def _extend_selection(self, point):
        if point != self._drag_origin:
            self._suppress_selection_click = True
            self.suppress_click()
        endpoint = self._offset_at(point)
        if endpoint != self._endpoint:
            self._endpoint = endpoint
            self.refresh()
            self.post_message(self.SelectionChanged(self))

    def on_mouse_move(self, event):
        if self.dragging:
            self._extend_selection(event.get_content_offset_capture(self))
            event.stop()
            event.prevent_default()

    def on_mouse_up(self, event):
        if event.button != 1 or not self.dragging:
            return
        self._extend_selection(event.get_content_offset_capture(self))
        self._dragging = False
        self._release_capture()
        if not self.has_selection:
            self.clear_selection()
        self.post_message(self.SelectionChanged(self))
        event.stop()
        event.prevent_default()

    def on_mouse_release(self):
        if (self.dragging and self._capture_app is not None
                and self._capture_app.mouse_captured is not self):
            self.clear_selection()

    def on_hide(self):
        self.clear_selection()

    def on_unmount(self):
        self._selection_closed = True
        self._pending_text = None
        self.clear_selection()
