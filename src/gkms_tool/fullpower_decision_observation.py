"""One typed decision shape for native observations and reconstructed teachers.

This is not ExamSaveData/89-field serialization. Origin/provenance is carried
separately and never upgrades simulator evidence into native evidence.
"""
from __future__ import annotations
from collections.abc import Mapping
from copy import deepcopy
from contextlib import closing
from dataclasses import asdict, dataclass, fields, replace
from functools import lru_cache
from pathlib import Path
import sqlite3

OBSERVATION_SCHEMA = "gkms.fullpower-decision-observation.v1"
TEACHER_STATE_SCHEMA = "gkms.plan3-simulator-decision-state.v1"
_SCOPE_REQUIRED = {'produce_id','plan_type','exam_effect_type','step_type','idol_card_id','character_id',
    'native_step_value','is_battle','flow'}


class FullPowerObservationUnavailable(ValueError):
    def __init__(self, gaps):
        self.gaps = tuple(gaps)
        super().__init__(";".join(self.gaps))


@lru_cache(maxsize=16)
def _master_source(database,master_dir,modified_ns,size):
    with closing(sqlite3.connect(Path(database).as_uri()+'?mode=ro',uri=True)) as connection:
        row=connection.execute("SELECT value FROM metadata WHERE key='source_dir'").fetchone()
    if row is None or Path(row[0]).resolve()!=Path(master_dir).resolve():
        raise FullPowerObservationUnavailable(("database-and-Master-directory-not-bound",))


def require_bound_master_paths(database,master_dir):
    """Require explicit matching importer provenance; never choose current DB."""
    database,master_dir=Path(database).resolve(),Path(master_dir).resolve()
    if not database.is_file() or not master_dir.is_dir():
        raise FullPowerObservationUnavailable(("version-Master-path-unavailable",))
    stat=database.stat();_master_source(str(database),str(master_dir),stat.st_mtime_ns,stat.st_size)
    return database,master_dir


@dataclass(frozen=True)
class FullPowerDecisionObservation:
    payload: Mapping
    source_origin: Mapping
    schema: str = OBSERVATION_SCHEMA

    def to_dict(self):
        return {"schema":self.schema,**deepcopy(dict(self.payload)),"source_origin":deepcopy(dict(self.source_origin))}


def _validate(payload):
    from .plan3_engine import Plan3State,Plan3ExamSettings
    from .plan3_native_state import Plan3NativeCard,Plan3NativeState
    gaps=[]
    for key in ("scalar","cards","inventory","settings","decision_scope"):
        if not isinstance(payload.get(key),Mapping):gaps.append("missing-mapping:"+key)
    if gaps:raise FullPowerObservationUnavailable(gaps)
    scalar,cards=payload["scalar"],payload["cards"]
    scope=payload['decision_scope']
    gaps.extend('missing-decision-scope:'+key for key in sorted(_SCOPE_REQUIRED-set(scope)))
    # Serialized typed fields must be present, even when their current value is
    # neutral. Dataclass defaults must not turn missing evidence into zero.
    gaps.extend("missing-scalar:"+key for key in sorted({f.name for f in fields(Plan3State)}-set(scalar)))
    gaps.extend("missing-native-carrier:"+key for key in sorted({f.name for f in fields(Plan3NativeState)}-set(cards)))
    gaps.extend("missing-setting:"+key for key in sorted({f.name for f in fields(Plan3ExamSettings)}-set(payload['settings'])))
    if payload["decision_scope"].get("plan_type")!="ProducePlanType_Plan3" or payload["decision_scope"].get("exam_effect_type")!="ProduceExamEffectType_ExamFullPower":
        gaps.append("fullpower-plan-or-effect-mismatch")
    if scope.get('is_battle')!=scalar.get('is_battle') or scope.get('native_step_value')!=scalar.get('step_type_value'):
        gaps.append('scalar-mode-scope-mismatch')
    if not payload["settings"]:gaps.append("settings-unavailable")
    schedule=payload.get("turn_parameter_schedule_remaining")
    if not isinstance(schedule,list) or any(type(v)is not int or v not in {1,2,3} for v in schedule):gaps.append("remaining-parameter-schedule-invalid")
    elif type(scalar.get("turns_remaining"))is int and len(schedule)!=scalar["turns_remaining"]:gaps.append("remaining-parameter-schedule-length-mismatch")
    if "gimmick_runtime" not in payload:gaps.append("gimmick-runtime-not-observed")
    drinks=payload["inventory"].get("drink_ids")
    if not isinstance(drinks,list) or any(not isinstance(d,str) or not d for d in drinks):gaps.append("current-drink-inventory-invalid")
    for zone,scalar_zone in (("hand","hand"),("deck","draw_pile"),("grave","discard_pile"),("lost","lost_pile"),("hold","hold_pile")):
        values=cards.get(zone);refs=scalar.get(scalar_zone)
        if not isinstance(values,list) or not isinstance(refs,list):
            gaps.append("ordered-zone-unavailable:"+zone);continue
        if any(not isinstance(card,Mapping) or not isinstance(card.get("guid"),str) or not card["guid"]
               or not isinstance(card.get("card_id"),str) or type(card.get("effective_upgrade"))is not int for card in values):
            gaps.append("ordered-card-identity-unavailable:"+zone);continue
        if [(card["card_id"],card["effective_upgrade"]) for card in values]!=[(r.get("card_id"),r.get("upgrade")) for r in refs]:
            gaps.append("scalar-native-zone-mismatch:"+zone)
        for index,card in enumerate(values):
            gaps.extend(f'missing-card:{zone}[{index}]:'+key for key in sorted({f.name for f in fields(Plan3NativeCard)}-set(card)))
    guids=[card.get("guid") for zone in ("hand","deck","grave","lost","hold") for card in cards.get(zone,()) if isinstance(card,Mapping)]
    if len(guids)!=len(set(guids)):gaps.append("duplicate-current-card-guid")
    scalar_enth=scalar.get("enthusiastic_runtime",{}).get("status")
    native_enth=cards.get("enthusiastic_runtime",{}).get("status")
    def enth_fields(value):
        return None if value is None else {key:value.get(key) for key in ("uid","value","turn","passing_turn_start")}
    if enth_fields(scalar_enth)!=enth_fields(native_enth):gaps.append("scalar-native-runtime-mismatch:enthusiastic_runtime")
    a,b=scalar.get("anti_debuff_runtime",{}),cards.get("anti_debuff_runtime",{})
    if a.get("count")!=b.get("count") or a.get("count",0)>0 and any(a.get(key)!=b.get(key) for key in ("uid","passing_turn_start")):
        gaps.append("scalar-native-runtime-mismatch:anti_debuff_runtime")
    if gaps:raise FullPowerObservationUnavailable(gaps)


def observation_from_mapping(state_payload, *, decision_scope, source_origin):
    if not isinstance(state_payload,Mapping):raise FullPowerObservationUnavailable(("state-payload-missing",))
    if state_payload.get("schema") not in {TEACHER_STATE_SCHEMA,OBSERVATION_SCHEMA}:
        raise FullPowerObservationUnavailable(("state-payload-schema-unrecognized",))
    payload={key:deepcopy(state_payload[key]) for key in ("scalar","cards","inventory","settings",
        "turn_parameter_schedule_remaining","gimmick_runtime") if key in state_payload}
    payload["decision_scope"]=deepcopy(dict(decision_scope))
    _validate(payload)
    return FullPowerDecisionObservation(payload,deepcopy(dict(source_origin)))


def observation_from_typed(state, native, *, inventory, settings, turn_parameter_schedule_remaining,
                           gimmick_runtime, decision_scope, source_origin):
    from .plan3_engine import Plan3State
    from .plan3_native_state import Plan3NativeState
    if not isinstance(state,Plan3State) or not isinstance(native,Plan3NativeState):
        raise TypeError("decision observation requires typed Plan3 scalar and native state")
    native.assert_plan3_projection(state)
    payload={"schema":OBSERVATION_SCHEMA,"scalar":state.to_dict(),"cards":asdict(native),
        "inventory":{"drink_ids":list(inventory.ids)},"settings":asdict(settings),
        "turn_parameter_schedule_remaining":list(turn_parameter_schedule_remaining),
        "gimmick_runtime":None if gimmick_runtime is None else asdict(gimmick_runtime)}
    # asdict retains tuples while serialized teacher mappings use lists.
    import json
    payload=json.loads(json.dumps(payload))
    return observation_from_mapping(payload,decision_scope=decision_scope,source_origin=source_origin)


def observation_from_native(raw_native, *, decision_scope, database:Path, master_dir:Path):
    """Read no game state: adapt a caller's existing native serializer result."""
    from .fullpower_native_observation import project_native_current_observation
    from .plan3_engine import load_plan3_exam_settings
    from .plan3_drink import load_plan3_drink_inventory
    from .nia_exam_save_runtime_hooks import build_nia_exam_save_runtime_hooks
    from .version_bound_master import bind_master_database
    database,master_dir=require_bound_master_paths(database,master_dir)
    expected={"produceId":decision_scope.get("produce_id"),"planType":4,"mainEffectType":47,
              "stepType":decision_scope.get("native_step_value"),'idolCardId':decision_scope.get('idol_card_id'),
              'characterId':decision_scope.get('character_id')}
    if any(raw_native.get(key)!=value for key,value in expected.items()):
        raise FullPowerObservationUnavailable(("native-mode-scope-mismatch",))
    with bind_master_database(Path(database)):
        try:
            projection=project_native_current_observation(raw_native,database=Path(database),master_dir=Path(master_dir))
        except FullPowerObservationUnavailable:
            raise
        except (ValueError,TypeError,KeyError,OSError) as error:
            raise FullPowerObservationUnavailable(("native-current-observation:"+type(error).__name__+":"+str(error),)) from error
        # The legacy scalar bridge predates the typed step field and leaves its
        # zero sentinel. Bind the caller's actual validated ExamSave value.
        if projection.state.step_type_value not in (0,raw_native['stepType']):
            raise FullPowerObservationUnavailable(('native-projected-step-mismatch',))
        state=replace(projection.state,step_type_value=raw_native['stepType'])
        settings=load_plan3_exam_settings(projection.parsed.setting_id,master_dir=Path(master_dir))
        inventory=load_plan3_drink_inventory(tuple(row["_id"] for row in raw_native["drinkList"]),database=Path(database),master_dir=Path(master_dir))
        if raw_native["produceId"]!="produce-004":
            raise FullPowerObservationUnavailable(("native-mode-extension-adapter-unavailable:"+raw_native["produceId"],))
        try:
            hooks=build_nia_exam_save_runtime_hooks(raw_native,projection.parsed,state,
                database=Path(database),master_dir=Path(master_dir))
        except (ValueError,TypeError,KeyError,OSError) as error:
            raise FullPowerObservationUnavailable(("native-gimmick-runtime:"+str(error),)) from error
        schedule=projection.parsed.turn_parameter_types[max(0,projection.parsed.current_turn-1):]
        return observation_from_typed(state,projection.native_state,inventory=inventory,settings=settings,
            turn_parameter_schedule_remaining=schedule,gimmick_runtime=hooks.runtime,decision_scope=decision_scope,
            source_origin={"kind":"native-projection","native_before_digest":projection.captured_digest,
                           "current_observation":projection.source_details})
