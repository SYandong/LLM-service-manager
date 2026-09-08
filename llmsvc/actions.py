# Generated-By: Codex / gpt-6-astra
"""Execution boundary reserved for M2; M1 never instantiates an executor."""

from typing import Any, Protocol

from llmsvc.state import Action


class ActionExecutor(Protocol):
    """Called under scheduler.action_lock after fresh protection validation.

    dry_run=True must invoke no writer, process action or persistent intent
    change. A wait must release the scheduler condition lock and revalidate
    the action after reacquiring it. Implementations arrive with M2.
    """

    def execute(self, action: Action, *, dry_run: bool) -> dict[str, Any]:
        ...
