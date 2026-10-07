# Generated-By: Codex / gpt-6.1-sol
"""Leave selection to the terminal and distinguish negotiated wheel input."""

from codecs import getincrementaldecoder
import os
import re
import selectors
import termios
import time
import tty

from textual import constants, events
from textual._parser import ParseError
from textual._xterm_parser import XTermParser
from textual.drivers.linux_driver import LinuxDriver


class FleetReplyFilter:
    """Remove late query replies without leaking their text into key actions."""

    DCS_PREFIX = b"\x1bP>|"
    PASTE_START = b"\x1b[200~"
    PASTE_END = b"\x1b[201~"
    MODE_REPLIES = tuple(b"\x1b[?" + str(mode).encode("ascii") + b";"
                         + str(state).encode("ascii") + b"$y"
                         for mode in (1, 1007) for state in range(5))
    MAX_REPLY_BYTES = 272

    def __init__(self):
        self.pending = bytearray()
        self.in_paste = False
        self.discard_dcs = False
        self.discard_csi = False
        self._interleaved_header = b""
        self._prefix_since = None

    @classmethod
    def _reply_prefix(cls, data):
        return (data == b"\x1b[" or cls.DCS_PREFIX.startswith(data)
                or data.startswith(cls.DCS_PREFIX)
                or re.fullmatch(rb"\x1b\[\?[0-9]*", data) is not None
                or re.match(rb"\x1b\[\?(?:[0-9]+u|(?:1|1007);)", data) is not None)

    def _restore_interleaved_header(self):
        if self._interleaved_header:
            self.pending[:0] = self._interleaved_header
            self._interleaved_header = b""

    def _known_reply(self):
        data = bytes(self.pending)
        # Keep completed introducers until the header distinguishes a reply
        # from an ordinary control sequence. A bare Escape still times out.
        return (bool(self._interleaved_header) or self.discard_dcs or self.discard_csi or data.startswith(self.DCS_PREFIX)
                or data in (b"\x1bP", b"\x1bP>", b"\x1b[")
                or data.startswith(b"\x1b[?"))

    def feed(self, data):
        if data:
            self._prefix_since = time.monotonic()
        self.pending.extend(data)
        output = bytearray()
        while self.pending:
            buffer = bytes(self.pending)
            if not self.in_paste and not self.discard_dcs and not self.discard_csi:
                if self._interleaved_header and not self._reply_prefix(buffer):
                    self._restore_interleaved_header()
                    continue
                if not self._interleaved_header:
                    header = next((header for header in (b"\x1bP>", b"\x1bP")
                                   if buffer.startswith(header + b"\x1b")), None)
                    if header is not None and self._reply_prefix(buffer[len(header):]):
                        # A query timeout can hand off a DCS introducer followed
                        # by another fragmented reply. Keep its bytes in order.
                        self._interleaved_header = header
                        del self.pending[:len(header)]
                        continue
            if self.discard_dcs:
                escape = buffer.find(b"\x1b")
                if escape < 0:
                    self.pending.clear()
                    break
                if escape + 1 == len(buffer):
                    del self.pending[:escape]
                    break
                # ST closes the discarded frame. A new escape resynchronizes.
                del self.pending[:escape + 2 if buffer[escape + 1] == 92 else escape]
                self.discard_dcs = False
                self._restore_interleaved_header()
                continue
            if self.discard_csi:
                final = next((index for index, byte in enumerate(buffer)
                              if 64 <= byte <= 126 or byte == 27), None)
                if final is None:
                    self.pending.clear()
                    break
                del self.pending[:final + (buffer[final] != 27)]
                self.discard_csi = False
                self._restore_interleaved_header()
                continue
            if self.in_paste:
                end = buffer.find(self.PASTE_END)
                if end >= 0:
                    end += len(self.PASTE_END)
                    output.extend(buffer[:end])
                    del self.pending[:end]
                    self.in_paste = False
                    continue
                keep = next((length for length in range(len(self.PASTE_END) - 1, 0, -1)
                             if buffer.endswith(self.PASTE_END[:length])), 0)
                output.extend(buffer[:-keep] if keep else buffer)
                if keep:
                    del self.pending[:-keep]
                else:
                    self.pending.clear()
                break
            if buffer[0] != 27:
                escape = buffer.find(b"\x1b")
                end = len(buffer) if escape < 0 else escape
                output.extend(buffer[:end])
                del self.pending[:end]
                continue
            if buffer.startswith(self.PASTE_START):
                output.extend(self.PASTE_START)
                del self.pending[:len(self.PASTE_START)]
                self.in_paste = True
                continue
            if buffer.startswith(self.DCS_PREFIX):
                escape = buffer.find(b"\x1b", len(self.DCS_PREFIX))
                if escape >= 0 and escape + 1 < len(buffer):
                    del self.pending[:escape + 2 if buffer[escape + 1] == 92 else escape]
                    self._restore_interleaved_header()
                    continue
                if len(buffer) > self.MAX_REPLY_BYTES:
                    self.discard_dcs = True
                    self.pending[:] = b"\x1b" if buffer.endswith(b"\x1b") else b""
                break
            reply = next((reply for reply in self.MODE_REPLIES if buffer.startswith(reply)), None)
            if reply is not None:
                del self.pending[:len(reply)]
                self._restore_interleaved_header()
                continue
            kitty = re.match(rb"\x1b\[\?[0-9]{1,10}u", buffer)
            if kitty is not None:
                del self.pending[:kitty.end()]
                self._restore_interleaved_header()
                continue
            overlong_flags = re.match(rb"\x1b\[\?[0-9]{11,}", buffer)
            if overlong_flags is not None:
                del self.pending[:overlong_flags.end()]
                self.discard_csi = True
                continue
            mode_prefix = re.match(rb"\x1b\[\?(?:1|1007);", buffer)
            if mode_prefix is not None:
                final = next((index for index in range(mode_prefix.end(), len(buffer))
                              if 64 <= buffer[index] <= 126 or buffer[index] == 27), None)
                if final is not None:
                    del self.pending[:final + (buffer[final] != 27)]
                    self._restore_interleaved_header()
                    continue
                if len(buffer) > self.MAX_REPLY_BYTES:
                    self.discard_csi = True
                    self.pending.clear()
                break
            if (re.fullmatch(rb"\x1b\[\?[0-9]+", buffer) is not None
                    or any(reply.startswith(buffer) for reply in self.MODE_REPLIES)):
                if len(buffer) > self.MAX_REPLY_BYTES:
                    self.discard_csi = True
                    self.pending.clear()
                break
            if any(prefix.startswith(buffer) for prefix in (self.DCS_PREFIX, self.PASTE_START)):
                break
            output.append(self.pending.pop(0))
        if not self.pending:
            self._prefix_since = None
        return bytes(output)

    def tick(self):
        # Recognized reply frames never expire into application keys. Ambiguous
        # prefixes still release a real Escape using Textual's normal delay.
        if (self.pending and not self.in_paste and not self._known_reply()
                and time.monotonic() - self._prefix_since >= getattr(constants, "ESCAPE_DELAY", 0.05)):
            data = bytes(self.pending)
            self.pending.clear()
            self._prefix_since = None
            return data
        return b""


class FleetXTermParser(XTermParser):
    """Keep raw SS3 wheel sequences distinct before Textual normalizes keys."""

    wheel_keys = False

    def _sequence_to_key_events(self, sequence, *args, **kwargs):
        if self.wheel_keys and sequence in ("\x1bOA", "\x1bOB"):
            yield events.Key("fleet_scroll_up" if sequence.endswith("A") else "fleet_scroll_down", None)
            return
        yield from super()._sequence_to_key_events(sequence, *args, **kwargs)


class FleetTerminalDriver(LinuxDriver):
    """Use iTerm2's alternate scroll without button or motion reporting."""

    QUERY_TIMEOUT = 0.2
    QUERY_BYTES = 65536
    STATE_QUERIES = {
        1: ("\x1b[?1$p", re.compile(rb"\x1b\[\?1;([0-4])\$y")),
        1007: ("\x1b[?1007$p", re.compile(rb"\x1b\[\?1007;([0-4])\$y")),
        "kitty": ("\x1b[?u", re.compile(rb"\x1b\[\?([0-9]{1,10})u")),
        "identity": ("\x1b[>0q", re.compile(rb"\x1bP>\|([^\x1b]{0,256})\x1b\\")),
    }
    # These are the exact pushes/pops in supported Textual 0.70 and 8.2.
    PARENT_KEYBOARD_WRITES = {"\x1b[>1u", "\x1b[>25u", "\x1b[<u"}

    def __init__(self, app, *, debug=False, mouse=False, size=None):
        super().__init__(app, debug=debug, mouse=False, size=size)
        self._scroll_query_done = False
        self._application_mode_open = False
        self._parent_mode_change = False
        self._previous_scroll_mode = None
        self._previous_cursor_mode = None
        self._kitty_protocol_open = False
        self._wheel_keys_enabled = False
        self._pending_input = bytearray()
        self._reply_filter = FleetReplyFilter()
        self._input_decoder = getincrementaldecoder("utf-8")()

    def write(self, data):
        # The fleet driver owns one negotiated alt-screen keyboard stack entry.
        # Suppress only the superclass's known lifecycle pushes and pop.
        if self._parent_mode_change and data in self.PARENT_KEYBOARD_WRITES:
            return
        super().write(data)

    def _query_terminal_state(self, states):
        """Query before the input thread starts, retaining unrelated input."""
        if not self.input_tty:
            return {}
        previous = termios.tcgetattr(self.fileno)
        attributes = list(previous)
        attributes[tty.CC] = list(previous[tty.CC])
        attributes[tty.IFLAG] = self._patch_iflag(attributes[tty.IFLAG])
        attributes[tty.LFLAG] = self._patch_lflag(attributes[tty.LFLAG])
        attributes[tty.CC][termios.VMIN] = 1
        attributes[tty.CC][termios.VTIME] = 0
        buffer = bytearray()
        replies = {}
        received = 0

        def outside_paste(position):
            in_paste = self._reply_filter.in_paste
            before = bytes(self._reply_filter.pending) + bytes(self._pending_input) + buffer[:position]
            for marker in re.finditer(rb"\x1b\[(200|201)~", before):
                in_paste = marker.group(1) == b"200"
            return not in_paste

        try:
            termios.tcsetattr(self.fileno, termios.TCSANOW, attributes)
            self.write("".join(self.STATE_QUERIES[state][0] for state in states))
            self.flush()
            deadline = time.monotonic() + self.QUERY_TIMEOUT
            with selectors.SelectSelector() as selector:
                selector.register(self.fileno, selectors.EVENT_READ)
                while received < self.QUERY_BYTES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        break
                    data = os.read(self.fileno, min(4096, self.QUERY_BYTES - received))
                    if not data:
                        break
                    received += len(data)
                    buffer.extend(data)
                    for state in states:
                        if state in replies:
                            continue
                        match = next((match for match in self.STATE_QUERIES[state][1].finditer(buffer)
                                      if outside_paste(match.start())), None)
                        if match is not None:
                            replies[state] = (match.group(1) if state == "identity"
                                              else int(match.group(1)))
                            del buffer[match.start():match.end()]
                    if len(replies) == len(states):
                        break
            return replies
        finally:
            self._pending_input.extend(buffer)
            termios.tcsetattr(self.fileno, termios.TCSANOW, previous)

    def _enable_mouse_support(self):
        # Both supported LinuxDrivers call this first with a ready writer,
        # before starting their input thread, then again after setup.
        if self._scroll_query_done:
            return
        self._scroll_query_done = True
        self.write("\x1b[?1000l\x1b[?1002l\x1b[?1003l")
        if getattr(constants, "DISABLE_KITTY_KEY", False):
            return
        previous = self._query_terminal_state((1, 1007, "kitty", "identity"))
        if (previous.get(1) not in (1, 2) or previous.get(1007) not in (1, 2)
                or "kitty" not in previous
                or re.fullmatch(rb"iTerm2 [0-9]+(?:\.[0-9]+)+[a-zA-Z0-9.+_-]*",
                                previous.get("identity", b"")) is None):
            return
        self._previous_cursor_mode = previous[1] == 1
        self._previous_scroll_mode = previous[1007] == 1
        # Push on the active alternate screen. Pop restores the entire prior
        # flag set; flags 2/8/16 must not change this app's key serialization.
        super().write("\x1b[>1u")
        self._kitty_protocol_open = True
        self.write("\x1b[?1h\x1b[?1007h")
        self.flush()
        states = (1, 1007, "kitty")
        confirmed = self._query_terminal_state(states)
        if all(confirmed.get(state) == 1 for state in states):
            self._wheel_keys_enabled = True
        else:
            self._restore_scroll_mode()

    def _restore_scroll_mode(self):
        self._wheel_keys_enabled = False
        controls = []
        if self._previous_scroll_mode is not None:
            controls.append("\x1b[?1007h" if self._previous_scroll_mode else "\x1b[?1007l")
        if self._previous_cursor_mode is not None:
            controls.append("\x1b[?1h" if self._previous_cursor_mode else "\x1b[?1l")
        if self._kitty_protocol_open:
            controls.append("\x1b[<u")
        if controls:
            super().write("".join(controls))
            # Queued restores must not be duplicated if flush later fails.
            self._previous_scroll_mode = None
            self._previous_cursor_mode = None
            self._kitty_protocol_open = False
            self.flush()

    def start_application_mode(self):
        self._scroll_query_done = False
        self._application_mode_open = True
        self._parent_mode_change = True
        try:
            super().start_application_mode()
        except BaseException as startup_error:
            try:
                self.stop_application_mode()
            except Exception as cleanup_error:
                raise startup_error from cleanup_error
            raise
        finally:
            self._parent_mode_change = False

    def stop_application_mode(self):
        if not self._application_mode_open:
            return
        try:
            self._restore_scroll_mode()
        finally:
            self._parent_mode_change = True
            try:
                super().stop_application_mode()
            finally:
                self._parent_mode_change = False
                self._application_mode_open = False

    def close(self):
        try:
            self._restore_scroll_mode()
        finally:
            super().close()

    def run_input_thread(self):
        """Feed buffered and subsequent bytes through the same Textual parser."""
        with selectors.SelectSelector() as selector:
            selector.register(self.fileno, selectors.EVENT_READ)

            def more_data():
                return bool(selector.select(0.1))

            # 0.70 uses a more-data callback; 8.2 advances timeouts via tick.
            parser = (FleetXTermParser(self._debug) if hasattr(XTermParser, "tick")
                      else FleetXTermParser(more_data, self._debug))
            process = getattr(self, "process_message", None) or self.process_event
            replies = self._reply_filter
            decode = self._input_decoder.decode

            def feed(data):
                decoded = decode(data)
                if decoded:
                    parser.wheel_keys = self._wheel_keys_enabled
                    for message in parser.feed(decoded):
                        process(message)

            try:
                pending = bytes(self._pending_input)
                self._pending_input.clear()
                if pending:
                    feed(replies.feed(pending))
                while not self.exit_event.is_set():
                    for _, mask in selector.select(0.1):
                        if mask & selectors.EVENT_READ:
                            data = os.read(self.fileno, 4096)
                            if not data:
                                return
                            feed(replies.feed(data))
                    # A response may split a UTF-8 character. Releasing its
                    # ambiguous Escape before the continuation corrupts input.
                    if not self._input_decoder.getstate()[0]:
                        feed(replies.tick())
                    if hasattr(parser, "tick") and not replies.pending:
                        for message in parser.tick():
                            process(message)
            finally:
                try:
                    for _ in parser.feed(""):
                        pass
                except (EOFError, ParseError):
                    pass
