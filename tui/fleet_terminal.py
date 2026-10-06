# Generated-By: Codex / gpt-6.1-sol
"""Leave drag selection to the terminal and translate its wheel into arrows."""

from codecs import getincrementaldecoder
import os
import re
import selectors
import termios
import time
import tty

from textual._parser import ParseError
from textual._xterm_parser import XTermParser
from textual.drivers.linux_driver import LinuxDriver


class FleetTerminalDriver(LinuxDriver):
    """Use alternate scroll without enabling button or motion reporting."""

    QUERY_TIMEOUT = 0.2
    QUERY_BYTES = 65536
    MODE_REPLY = re.compile(rb"\x1b\[\?1007;([0-4])\$y")

    def __init__(self, app, *, debug=False, mouse=False, size=None):
        super().__init__(app, debug=debug, mouse=False, size=size)
        self._scroll_query_done = False
        self._application_mode_open = False
        self._previous_scroll_mode = None
        self._pending_input = bytearray()

    def _query_scroll_mode(self):
        """Query before the input thread starts, retaining unrelated input."""
        if not self.input_tty:
            return None
        previous = termios.tcgetattr(self.fileno)
        attributes = list(previous)
        attributes[tty.CC] = list(previous[tty.CC])
        attributes[tty.IFLAG] = self._patch_iflag(attributes[tty.IFLAG])
        attributes[tty.LFLAG] = self._patch_lflag(attributes[tty.LFLAG])
        attributes[tty.CC][termios.VMIN] = 1
        attributes[tty.CC][termios.VTIME] = 0
        buffer = bytearray()
        try:
            termios.tcsetattr(self.fileno, termios.TCSANOW, attributes)
            self.write("\x1b[?1007$p")
            self.flush()
            deadline = time.monotonic() + self.QUERY_TIMEOUT
            with selectors.SelectSelector() as selector:
                selector.register(self.fileno, selectors.EVENT_READ)
                while len(buffer) < self.QUERY_BYTES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        break
                    data = os.read(self.fileno, min(4096, self.QUERY_BYTES - len(buffer)))
                    if not data:
                        break
                    buffer.extend(data)
                    match = self.MODE_REPLY.search(buffer)
                    if match is not None:
                        state = match.group(1)
                        del buffer[match.start():match.end()]
                        return state == b"1" if state in (b"1", b"2") else None
            return None
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
        self._previous_scroll_mode = self._query_scroll_mode()
        if self._previous_scroll_mode is not None:
            self.write("\x1b[?1007h")
            self.flush()

    def _restore_scroll_mode(self):
        if self._previous_scroll_mode is not None:
            self.write("\x1b[?1007h" if self._previous_scroll_mode else "\x1b[?1007l")
            self.flush()
            self._previous_scroll_mode = None

    def start_application_mode(self):
        self._scroll_query_done = False
        self._application_mode_open = True
        try:
            super().start_application_mode()
        except BaseException as startup_error:
            try:
                self.stop_application_mode()
            except Exception as cleanup_error:
                raise startup_error from cleanup_error
            raise

    def stop_application_mode(self):
        if not self._application_mode_open:
            return
        try:
            self._restore_scroll_mode()
        finally:
            try:
                super().stop_application_mode()
            finally:
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
            parser = (XTermParser(self._debug) if hasattr(XTermParser, "tick")
                      else XTermParser(more_data, self._debug))
            process = getattr(self, "process_message", None) or self.process_event
            decode = getincrementaldecoder("utf-8")().decode

            def feed(data):
                for message in parser.feed(decode(data)):
                    process(message)

            try:
                pending = bytes(self._pending_input)
                self._pending_input.clear()
                if pending:
                    feed(pending)
                while not self.exit_event.is_set():
                    for _, mask in selector.select(0.1):
                        if mask & selectors.EVENT_READ:
                            data = os.read(self.fileno, 4096)
                            if not data:
                                return
                            feed(data)
                    if hasattr(parser, "tick"):
                        for message in parser.tick():
                            process(message)
            finally:
                try:
                    for _ in parser.feed(""):
                        pass
                except (EOFError, ParseError):
                    pass
