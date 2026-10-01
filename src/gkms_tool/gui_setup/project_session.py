"""V12 session lifecycle composed into this app's controller instances only."""
from __future__ import annotations

from functools import wraps
import threading

from .native_observation import observe_native_outcomes, observe_safely
from .session import _phase


MODE_LABELS = {"produce-001": "初 Regular", "produce-002": "初 Pro",
               "produce-003": "初 Master", "produce-004": "N.I.A. Pro",
               "produce-005": "N.I.A. Master"}


def attach_controller(app, journal):
    """Observe terminal poll before the existing batch can start another run.

    Attach once on the Tk owner before any action, and detach after its worker
    has stopped. Results/errors keep their original semantics. Only methods on
    these two supplied instances are wrapped; no global controller or gateway
    classes are changed.
    """
    live, batch = app._console_live_controller, app._console_batch_controller
    if getattr(live, "_gui_session_observation", None) is not None:
        raise RuntimeError("Session observation is already attached to this controller.")
    if _phase(live.view.phase) in {"running", "cancelling"} or batch.view.running:
        raise RuntimeError("Attach session observation before starting cultivation.")
    owner = threading.get_ident()
    marker = object()
    changes = []

    def replace(obj, name, builder):
        original = getattr(obj, name)
        had_local = name in vars(obj)
        replacement = builder(original)
        changes.append((obj, name, original, replacement, had_local))
        setattr(obj, name, replacement)

    def identity():
        request = live.request
        idol = getattr(request, "idol_card_id", "")
        mode = getattr(request, "produce_id", "")
        label = getattr(app, "profile_label_by_id", {}).get(idol, idol)
        return dict(character=label, mode=MODE_LABELS.get(mode, mode),
                    idol_id=idol, produce_id=mode,
                    run_id=getattr(request, "expected_run_id", None))

    def remember_terminal(view, source_run_id=None):
        remember = getattr(journal, 'remember_terminal_display', None)
        if callable(remember):
            observe_safely(journal, remember, view, source_run_id=source_run_id)

    def start_wrapper(original):
        @wraps(original)
        def start(*args, **kwargs):
            if _phase(live.view.phase) in {"running", "cancelling"}:
                return original(*args, **kwargs)
            if not batch.view.running:
                observe_safely(journal, journal.begin_batch, 1)
            observe_safely(journal, journal.begin, **identity())
            try:
                value = original(*args, **kwargs)
            except Exception:
                observe_safely(journal, journal.finish, "failed")
                raise
            remember_terminal(value, getattr(live.request, 'expected_run_id', None))
            observe_safely(journal, journal.finish, value.phase)
            return value
        return start

    def view_wrapper(original):
        @wraps(original)
        def observe(*args, **kwargs):
            value = original(*args, **kwargs)
            progress = getattr(live, "_progress", None)
            if isinstance(progress, dict):
                observe_safely(journal, journal.bind_run, progress.get("source_run_id"))
            remember_terminal(value, progress.get("source_run_id") if isinstance(progress, dict) else None)
            observe_safely(journal, journal.finish, value.phase)
            return value
        return observe

    def batch_wrapper(original):
        @wraps(original)
        def start(target_cycles=1):
            if not batch.view.running and type(target_cycles) is int and 1 <= target_cycles <= 999:
                observe_safely(journal, journal.begin_batch, target_cycles)
            return original(target_cycles)
        return start

    def runner_wrapper(original):
        @wraps(original)
        def run(*args, **kwargs):
            forward = kwargs.get("progress_callback")
            def progress(value):
                if isinstance(value, dict):
                    observe_safely(journal, journal.bind_run, value.get("source_run_id"))
                    remember = getattr(journal, 'remember_model_progress', None)
                    if callable(remember):
                        observe_safely(journal, remember, value)
                if forward is not None:
                    return forward(value)
            kwargs["progress_callback"] = progress
            observe_safely(journal, journal.bind_run, kwargs.get("expected_run_id"))
            with observe_native_outcomes(journal):
                return original(*args, **kwargs)
        return run

    try:
        replace(live, "start", start_wrapper)
        replace(live, "poll", view_wrapper)
        replace(live, "stop", view_wrapper)
        replace(batch, "start", batch_wrapper)
        if callable(getattr(live, "_runner", None)):
            replace(live, "_runner", runner_wrapper)
        live._gui_session_observation = marker
    except Exception:
        for obj, name, original, replacement, had_local in reversed(changes):
            if getattr(obj, name) is replacement:
                if had_local:
                    setattr(obj, name, original)
                else:
                    delattr(obj, name)
        raise

    def detach():
        if threading.get_ident() != owner:
            raise RuntimeError("Detach session observation on the Tk owner thread.")
        if _phase(live.view.phase) in {"running", "cancelling"} or batch.view.running:
            raise RuntimeError("Stop the controller before detaching session observation.")
        for obj, name, original, replacement, had_local in reversed(changes):
            if getattr(obj, name) is replacement:
                if had_local:
                    setattr(obj, name, original)
                else:
                    delattr(obj, name)
        if getattr(live, "_gui_session_observation", None) is marker:
            delattr(live, "_gui_session_observation")
    return detach
