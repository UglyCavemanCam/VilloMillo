"""The server's event loop drops Windows' report of a connection the other
side already closed (WinError 10054), which filled job logs with tracebacks,
and nothing else (server/api.py)."""

import pytest

pytest.importorskip("fastapi")

from server.api import _quiet_connection_resets


class Loop:
    def __init__(self):
        self.reported = []

    def default_exception_handler(self, context):
        self.reported.append(context)


def test_a_reset_on_a_closed_connection_is_dropped():
    loop = Loop()
    _quiet_connection_resets(loop, {
        "message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
        "exception": ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host"),
    })
    assert loop.reported == []


def test_anything_else_is_still_reported():
    loop = Loop()
    other = {"message": "Task exception was never retrieved", "exception": ValueError("boom")}
    reset_elsewhere = {"message": "Exception in callback something_else()", "exception": ConnectionResetError()}
    _quiet_connection_resets(loop, other)
    _quiet_connection_resets(loop, reset_elsewhere)
    assert loop.reported == [other, reset_elsewhere]
