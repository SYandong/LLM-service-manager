# Generated-By: Codex / gpt-6.1-sol
"""Opt-in observation and declarations for independently operated services."""


class FleetError(RuntimeError):
    def __init__(self, status, error):
        super().__init__(error)
        self.status = status
        self.error = error
