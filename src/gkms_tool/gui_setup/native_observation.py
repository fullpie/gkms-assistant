"""Scoped observation of values already returned by the native gateway.

No game reads, writes, retries or class monkeypatches. The controller worker
enters the scope; unrelated workers and gateway callers have no observer.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps


_journal = ContextVar("gkms_gui_session_native_observer", default=None)


def observe_safely(journal, function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception:
        # Display recording failure must never replace the original result or
        # disrupt pending-transaction handling in the real controller.
        journal.observation_errors += 1
        return None


@contextmanager
def observe_native_outcomes(journal):
    token = _journal.set(journal)
    try:
        yield
    finally:
        _journal.reset(token)


def observe_native_return(*, require_settled: bool):
    """Explicit decorator for the gateway's existing public return boundary."""
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            value = function(*args, **kwargs)
            journal = _journal.get()
            if journal is not None:
                observe_safely(journal, journal.observe_native_outcome, value,
                               require_settled=require_settled)
            return value
        return call
    return decorate
