"""Physical-only Plan1 adapter over the existing generic Maa dispatchers.

The learned runtime supplies one candidate already selected from Plan1's
complete native legal set.  This adapter only binds that identity to the
current ExamSave/capture, reuses the established card, drink, or END_TURN Maa
dispatcher, and waits for a changed settled native state.  It owns no ranking
and never calls the whole-exam Maa policy.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import time
from typing import Any, Final

from .live_actions import SuggestedClick
from .plan1_learned_policy_runtime import Plan1MaaExecutionReceipt
from .unified_legal_action_snapshot import UnifiedLegalActionSnapshot


SCHEMA: Final = "gkms.plan1-existing-maa-single-action-executor.v1"


def _field(value: object, *names: str) -> object | None:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _digest(value: object) -> str | None:
    digest = getattr(value, "digest", None)
    if not callable(digest):
        return None
    try:
        result = digest()
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None
    return result if isinstance(result, str) and result else None


def _state(value: object) -> object:
    state = _field(value, "state")
    return value if state is None else state


def _state_mapping(value: object) -> Mapping[str, object] | None:
    state = _state(value)
    if isinstance(state, Mapping):
        return state
    to_dict = getattr(state, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None
        return result if isinstance(result, Mapping) else None
    return None


def _opaque(value: object) -> Mapping[str, object] | None:
    runtime = _field(_state(value), "root_runtime")
    opaque = _field(runtime, "opaque_fields")
    to_value = getattr(opaque, "to_value", None)
    if callable(to_value):
        try:
            opaque = to_value()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None
    return opaque if isinstance(opaque, Mapping) else None


def _settled(value: object) -> bool:
    state = _state(value)
    if _field(state, "is_native_actionable_settled") is True:
        return True
    runtime = _field(state, "root_runtime")
    return bool(_field(runtime, "is_exam_end_complete") is True)


def _same_exam(before: object, after: object) -> bool:
    """Keep post-input settlement inside the exact cultivation stage."""

    for name in ("run_id", "source_path"):
        left = _field(before, name)
        right = _field(after, name)
        if not isinstance(left, str) or not left or left != right:
            return False
    left_state = _state(before)
    right_state = _state(after)
    for name in (
        "character_id",
        "setting_id",
        "exam_type",
        "step_type_value",
        "limit_turn",
    ):
        if _field(left_state, name) != _field(right_state, name):
            return False
    left_opaque = _opaque(before)
    right_opaque = _opaque(after)
    if left_opaque is None or right_opaque is None:
        return False
    for names in (
        ("produceId", "produce_id"),
        ("planType", "plan_type"),
        ("mainEffectType", "main_effect_type"),
    ):
        left = next((left_opaque[name] for name in names if name in left_opaque), None)
        right = next((right_opaque[name] for name in names if name in right_opaque), None)
        if left != right:
            return False
    return True


def _capture_suggestion(
    capture: Mapping[str, object],
    *,
    box: tuple[int, int, int, int],
    label: str,
) -> SuggestedClick:
    left, top, right, bottom = box
    return SuggestedClick(
        label=label,
        canonical_x=(left + right) // 2,
        canonical_y=(top + bottom) // 2,
        source_png_path=str(capture["png_path"]),
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=box,
        click_count=1,
    )


class Plan1ExistingMaaSingleActionExecutor:
    """Route one selected Plan1 action to existing physical Maa components."""

    def __init__(
        self,
        *,
        evidence_reader: Callable[[], object],
        command_sender: Callable[..., Mapping[str, object]] | None = None,
        screen_binder: Callable[..., object] | None = None,
        card_dispatcher: Callable[..., object] | None = None,
        drink_dispatcher: Callable[..., object] | None = None,
        end_turn_dispatcher: object | None = None,
        drink_confirmation_effect_policy: (
            Callable[[Mapping[str, object], object], bool | None] | None
        ) = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_seconds: float = 0.25,
        max_settlement_polls: int = 120,
    ) -> None:
        if not callable(evidence_reader):
            raise TypeError("evidence_reader must be callable")
        if command_sender is None:
            from .controller_client import send_command

            command_sender = send_command
        if not callable(command_sender):
            raise TypeError("command_sender must be callable")
        if screen_binder is None:
            from .plan2_native_maa_action_executor import (
                MaaExamSaveSlotScreenBinder,
            )

            screen_binder = MaaExamSaveSlotScreenBinder(
                command_sender=command_sender
            )
        if card_dispatcher is None:
            from .plan2_native_maa_action_executor import (
                MaaExamSaveCardPlayDispatcher,
            )

            card_dispatcher = MaaExamSaveCardPlayDispatcher(
                command_sender=command_sender,
                sleep=sleep,
            )
        if drink_dispatcher is None:
            from .plan2_native_maa_action_executor import MaaExistingDrinkDispatcher

            drink_dispatcher = MaaExistingDrinkDispatcher(
                command_sender=command_sender,
                sleep=sleep,
            )
        if end_turn_dispatcher is None:
            from .plan2_native_maa_action_executor import MaaExistingEndTurnDispatcher

            end_turn_dispatcher = MaaExistingEndTurnDispatcher(
                command_sender=command_sender,
                sleep=sleep,
            )
        for name, value in (
            ("screen_binder", screen_binder),
            ("card_dispatcher", card_dispatcher),
            ("drink_dispatcher", drink_dispatcher),
            ("sleep", sleep),
        ):
            if not callable(value):
                raise TypeError(f"{name} must be callable")
        if not callable(getattr(end_turn_dispatcher, "dispatch_current", None)):
            raise TypeError("end_turn_dispatcher must expose dispatch_current")
        if drink_confirmation_effect_policy is not None and not callable(
            drink_confirmation_effect_policy
        ):
            raise TypeError("drink_confirmation_effect_policy must be callable or None")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        if type(max_settlement_polls) is not int or max_settlement_polls < 1:
            raise ValueError("max_settlement_polls must be a positive integer")
        self.evidence_reader = evidence_reader
        self.command_sender = command_sender
        self.screen_binder = screen_binder
        self.card_dispatcher = card_dispatcher
        self.drink_dispatcher = drink_dispatcher
        self.end_turn_dispatcher = end_turn_dispatcher
        self.drink_confirmation_effect_policy = drink_confirmation_effect_policy
        self.sleep = sleep
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.max_settlement_polls = max_settlement_polls

    @staticmethod
    def _candidate_kind(candidate: Mapping[str, object]) -> str:
        kind = candidate.get("kind")
        if kind not in {"play", "drink", "end_turn"}:
            raise ValueError("selected Plan1 candidate kind is unsupported")
        return str(kind)

    @staticmethod
    def _authorize_candidate(
        candidate: Mapping[str, object],
        evidence: object,
        *,
        before_digest: str,
    ) -> None:
        from .plan2_native_maa_action_executor import Plan2InputStateAdvanced

        if _digest(evidence) != before_digest:
            raise Plan2InputStateAdvanced(
                "Plan1 selected boundary advanced",
                input_submitted=False,
            )
        kind = Plan1ExistingMaaSingleActionExecutor._candidate_kind(candidate)
        if kind == "end_turn":
            if not _settled(evidence):
                raise Plan2InputStateAdvanced(
                    "Plan1 END_TURN boundary is not settled",
                    input_submitted=False,
                )
            return
        if kind == "play":
            slot = candidate.get("slot")
            guid = candidate.get("card_guid", candidate.get("guid"))
            card_id = candidate.get("card_id")
            hand = _field(_field(_state(evidence), "zones"), "hand")
            if (
                type(slot) is not int
                or not isinstance(hand, (tuple, list))
                or not 0 <= slot < len(hand)
            ):
                raise Plan2InputStateAdvanced(
                    "Plan1 selected Hand slot disappeared",
                    input_submitted=False,
                )
            card = hand[slot]
            if _field(card, "guid") != guid or _field(card, "card_id", "id") != card_id:
                raise Plan2InputStateAdvanced(
                    "Plan1 selected card identity changed",
                    input_submitted=False,
                )
            return
        slot = candidate.get("slot_index")
        drink_id = candidate.get("drink_id")
        instance_id = candidate.get("instance_id")
        opaque = _opaque(evidence)
        inventory = None if opaque is None else opaque.get("drinkList")
        if (
            type(slot) is not int
            or not isinstance(inventory, list)
            or not 0 <= slot < len(inventory)
            or not isinstance(inventory[slot], Mapping)
        ):
            raise Plan2InputStateAdvanced(
                "Plan1 selected drink slot disappeared",
                input_submitted=False,
            )
        current_id = inventory[slot].get(
            "_id", inventory[slot].get("id", inventory[slot].get("drink_id"))
        )
        raw_instance = inventory[slot].get(
            "_instanceId",
            inventory[slot].get(
                "instance_id",
                inventory[slot].get(
                    "instanceId",
                    inventory[slot].get(
                        "_uid", inventory[slot].get("uid")
                    ),
                ),
            ),
        )
        current_instance = (
            str(raw_instance)
            if isinstance(raw_instance, (str, int))
            and not isinstance(raw_instance, bool)
            and str(raw_instance)
            else f"localsave-drink:{slot}:{current_id}"
        )
        if current_id != drink_id or current_instance != instance_id:
            raise Plan2InputStateAdvanced(
                "Plan1 selected drink identity changed",
                input_submitted=False,
            )

    def _wait_after(self, before: object) -> object | None:
        before_digest = _digest(before)
        if before_digest is None:
            return None
        for poll in range(self.max_settlement_polls):
            try:
                current = self.evidence_reader()
            except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
                current = None
            if (
                current is not None
                and _digest(current) not in {None, before_digest}
                and _same_exam(before, current)
                and _settled(current)
            ):
                return current
            if poll + 1 < self.max_settlement_polls:
                self.sleep(self.poll_interval_seconds)
        return None

    def _dispatch(
        self,
        candidate: Mapping[str, object],
        evidence: object,
        authorize: Callable[[Mapping[str, object]], None],
    ) -> object:
        kind = self._candidate_kind(candidate)
        if kind == "play":
            slot = candidate.get("slot")
            if type(slot) is not int:
                raise ValueError("Plan1 PLAY candidate has no exact Hand slot")
            binding = self.screen_binder(evidence, expected_slot=slot)
            boxes = _field(binding, "hand_boxes")
            capture = _field(binding, "capture")
            if (
                not isinstance(boxes, (tuple, list))
                or not 0 <= slot < len(boxes)
                or not isinstance(capture, Mapping)
            ):
                raise ValueError("Plan1 Maa screen binding is incomplete")
            suggestion = _capture_suggestion(
                capture,
                box=tuple(boxes[slot]),
                label=f"Plan1 learned PLAY:{candidate.get('card_guid')}",
            )
            return self.card_dispatcher(
                suggestion,
                None,
                pre_input_authorizer=authorize,
            )
        if kind == "drink":
            from .plan2_native_horizon import Plan2NativeDrinkAction
            from .plan3_audition_executor import DRINK_SLOT_BOXES

            slot = candidate.get("slot_index")
            instance = candidate.get("instance_id")
            drink_id = candidate.get("drink_id")
            selected = candidate.get("selected_card_guid", "")
            if (
                type(slot) is not int
                or not 0 <= slot < len(DRINK_SLOT_BOXES)
                or not isinstance(instance, str)
                or not instance
                or not isinstance(drink_id, str)
                or not drink_id
                or not isinstance(selected, str)
            ):
                raise ValueError("Plan1 DRINK candidate identity is incomplete")
            capture = dict(
                self.command_sender("capture_screen_once", timeout=15.0)
            )
            suggestion = _capture_suggestion(
                capture,
                box=DRINK_SLOT_BOXES[slot],
                label=f"Plan1 learned DRINK:{slot}:{drink_id}",
            )
            effect = (
                None
                if self.drink_confirmation_effect_policy is None
                else self.drink_confirmation_effect_policy(candidate, evidence)
            )
            if effect is not None and type(effect) is not bool:
                raise TypeError("drink confirmation policy must return bool or None")
            action = Plan2NativeDrinkAction(
                slot,
                instance,
                drink_id,
                selected_card_guid=selected,
            )
            return self.drink_dispatcher(
                action,
                suggestion,
                pre_input_authorizer=authorize,
                confirmation_has_effect=effect,
            )
        return self.end_turn_dispatcher.dispatch_current(
            pre_input_authorizer=authorize
        )

    def __call__(
        self,
        *,
        candidate: Mapping[str, object],
        evidence: object,
        snapshot: UnifiedLegalActionSnapshot,
    ) -> Plan1MaaExecutionReceipt:
        if not isinstance(candidate, Mapping):
            raise TypeError("candidate must be a mapping")
        if not isinstance(snapshot, UnifiedLegalActionSnapshot) or not snapshot.complete:
            raise TypeError("snapshot must be one complete unified set")
        action_id = candidate.get("action_id")
        if not isinstance(action_id, str) or action_id not in snapshot.action_ids:
            raise ValueError("selected action is outside the exact snapshot")
        before_digest = _digest(evidence)
        if before_digest != snapshot.boundary_digest:
            raise ValueError("Plan1 executor evidence differs from learned boundary")

        def authorize(_capture: Mapping[str, object]) -> None:
            current = self.evidence_reader()
            self._authorize_candidate(
                candidate,
                current,
                before_digest=before_digest,
            )

        from .plan2_native_maa_action_executor import Plan2InputStateAdvanced

        dispatch: object | None = None
        advanced_submitted = False
        try:
            dispatch = self._dispatch(candidate, evidence, authorize)
        except Plan2InputStateAdvanced as error:
            advanced_submitted = bool(getattr(error, "input_submitted", False))
            if not advanced_submitted:
                return Plan1MaaExecutionReceipt(
                    action_id=action_id,
                    boundary_before_digest=before_digest,
                    input_submitted=False,
                    blockers=("pre-input-boundary-advanced",),
                )
            dispatch_proof: Mapping[str, object] = {
                "schema": SCHEMA,
                "action_id": action_id,
                "input_state_advanced": str(error),
            }
        else:
            submitted = _field(dispatch, "submitted", "input_submitted") is True
            to_dict = getattr(dispatch, "to_dict", None)
            raw = to_dict() if callable(to_dict) else dispatch
            dispatch_proof = {
                "schema": SCHEMA,
                "action_id": action_id,
                "dispatch": dict(raw) if isinstance(raw, Mapping) else {},
            }
            if not submitted:
                return Plan1MaaExecutionReceipt(
                    action_id=action_id,
                    boundary_before_digest=before_digest,
                    input_submitted=False,
                    blockers=("maa-dispatch-not-submitted",),
                )

        after = self._wait_after(evidence)
        if after is None:
            return Plan1MaaExecutionReceipt(
                action_id=action_id,
                boundary_before_digest=before_digest,
                input_submitted=True,
                submission_proof=dispatch_proof,
                blockers=("settled-state-after-unavailable",),
            )
        after_digest = _digest(after)
        after_state = _state_mapping(after)
        if after_digest is None or after_state is None:
            return Plan1MaaExecutionReceipt(
                action_id=action_id,
                boundary_before_digest=before_digest,
                input_submitted=True,
                submission_proof=dispatch_proof,
                blockers=("native-state-after-invalid",),
            )
        return Plan1MaaExecutionReceipt(
            action_id=action_id,
            boundary_before_digest=before_digest,
            input_submitted=True,
            boundary_after_digest=after_digest,
            state_after=after_state,
            submission_proof=dispatch_proof,
        )


def bind_plan1_existing_maa_learned_runtime(
    *,
    evidence_reader: Callable[[], object],
    bundle: object,
    **executor_options: object,
) -> object:
    """Bind this physical adapter to one explicit learned policy bundle."""

    from .plan1_learned_policy_runtime import bind_plan1_learned_policy_runtime

    executor = Plan1ExistingMaaSingleActionExecutor(
        evidence_reader=evidence_reader,
        **executor_options,
    )
    return bind_plan1_learned_policy_runtime(executor, bundle=bundle)


def bind_active_plan1_existing_maa_learned_runtime(
    *,
    evidence_reader: Callable[[], object],
    **executor_options: object,
) -> object:
    """Bind this physical adapter to the current active policy bundle."""

    from .plan1_learned_policy_runtime import (
        bind_active_plan1_learned_policy_runtime,
    )

    executor = Plan1ExistingMaaSingleActionExecutor(
        evidence_reader=evidence_reader,
        **executor_options,
    )
    return bind_active_plan1_learned_policy_runtime(executor)


__all__ = [
    "Plan1ExistingMaaSingleActionExecutor",
    "SCHEMA",
    "bind_active_plan1_existing_maa_learned_runtime",
    "bind_plan1_existing_maa_learned_runtime",
]
