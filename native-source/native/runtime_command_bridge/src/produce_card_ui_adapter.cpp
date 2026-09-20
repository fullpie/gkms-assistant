#include "produce_card_ui_adapter.hpp"
#include "screen_context.hpp"
#include "pointer_identity.hpp"
#include "customize_budget.hpp"
#include "reward_group.hpp"
#include "continuation.hpp"
#include "exam_selector_close_owner.hpp"
#include "exam_selector_diagnostic.hpp"
#include "exam_model_observation.hpp"
#include <algorithm>
#include <array>

namespace gkms::bridge {
namespace {
bool flag(Runtime& r, void* o, const char* name) { return r.unbox<bool>(r.getter(o, name)); }
int integer(Runtime& r, void* o, const char* name) { return r.unbox<int>(r.getter(o, name)); }
std::string text(Runtime& r, void* o, const char* name) { return r.string(r.getter(o, name)); }
void* optional_getter(Runtime& r, void* o, const char* name) {
    void* method{};
    try { method = r.method(r.object_class(o), name, 0); }
    catch (const std::runtime_error& error) {
        if (std::string(error.what()).find("method contract unavailable:") != 0) throw;
        return nullptr;
    }
    return r.invoke(method, o);
}
json integer_sequence(Runtime& r, void* values) {
    json result = json::array();
    if (!values) return result;
    // GetEnumerator boxes array/list scalar values through the normal managed
    // ABI; do not treat Int32[] storage as an array of object pointers.
    auto enumerator = r.getter(values, "GetEnumerator");
    while (flag(r, enumerator, "MoveNext")) {
        if (result.size() >= 256) throw std::runtime_error("native customize count list exceeds bound");
        result.push_back(integer(r, enumerator, "get_Current"));
    }
    auto dispose = optional_getter(r, enumerator, "Dispose");
    (void)dispose;
    return result;
}
bool active(Runtime& r, void* object) {
    return object && flag(r, r.getter(object, "get_gameObject"), "get_activeInHierarchy");
}
std::string instance_id(void* object) {
    return pointer_identity(object);
}
bool clickable(Runtime& r, void* button) {
    return active(r, button) && flag(r, button, "get_IsEnabled") && !flag(r, button, "get_IsDisabled") &&
           r.read_object_field(button, "onClickedCallback");
}
void click(Runtime& r, void* button) {
    if (!clickable(r, button)) throw std::runtime_error("native card UI button is no longer enabled");
    r.invoke(r.method(r.object_class(button), "OnClickedHandler", 0), button);
}
json action(const char* name, json target) {
    target["action_id"] = name;
    return {{"action_id", name}, {"target", std::move(target)}};
}
json schedule_customize_lifecycle(Runtime& r){
    auto user=r.klass("Assembly-CSharp","Campus.Common.User","UserDataManager");
    auto progress=r.invoke(r.method(user,"get_UserProduceProgress",0),nullptr);
    if(!progress)return {{"input_ready",false},{"phase","awaiting_native_progress"},{"in_progress_step",nullptr}};
    const bool started=r.unbox<bool>(r.invoke(r.method(r.object_class(progress),"get_InProgressStep",0,0x06019FFD),progress));
    const int step=integer(r,progress,"get_StepType"),status=integer(r,progress,"get_Status");
    const bool ready=schedule_customize_step_ready(started,step,status);
    return {{"input_ready",ready},{"in_progress_step",started},{"step_type",step},{"progress_status",status},
        {"produce_id",text(r,progress,"get_ProduceId")},{"week",integer(r,progress,"get_StepNumber")},
        {"phase",ready?"customize_step_started":status==4?"awaiting_customize_exit":"awaiting_customize_start"},
        {"source","native UserProduceProgress.InProgressStep; NonBlockOpenInAsync Start-request branch"}};
}
json schedule_customize_finish_canvas(Runtime& r,void* pview){
    auto canvas=r.read_object_field(pview,"_goNextButtonCanvasGroup");
    const bool shown=active(r,canvas),interactable=canvas&&flag(r,canvas,"get_interactable"),raycasts=canvas&&flag(r,canvas,"get_blocksRaycasts");
    const float alpha=canvas?r.unbox<float>(r.getter(canvas,"get_alpha")):0.f;
    return {{"active",shown},{"alpha",alpha},{"interactable",interactable},{"blocks_raycasts",raycasts},
        {"input_ready",schedule_customize_finish_canvas_ready(shown,alpha,interactable,raycasts)},
        {"source","ScheduleCustomizeScreenView._goNextButtonCanvasGroup; post-Start OnSelectCardScreenAnimationAsync.DOFadeBlocking"}};
}
void* view(Runtime& r, void* presenter) { return r.read_object_field(presenter, "_view"); }
void* common_button(Runtime& r, void* presenter, const char* property) {
    return r.getter(r.getter(view(r, presenter), "get_CommonView"), property);
}
std::vector<void*> children(Runtime& r, void* presenter, const char* assembly, const char* ns, const char* name) {
    auto component = r.klass("UnityEngine.CoreModule.dll", "UnityEngine", "Component");
    auto requested = r.reflection_type(r.klass(assembly, ns, name));
    bool include_inactive = false;
    auto list = r.invoke(r.method(component, "GetComponentsInChildren", 2, 0x060013C4), presenter,
                         {requested, &include_inactive});
    return r.enumerate(list, 256);
}
void* reward_view(Runtime& r, void* presenter) {
    void* selected{};
    const auto kind = r.class_name(r.object_class(presenter));
    std::vector<void*> candidates;
    if(kind=="ScheduleFanPresentScreenPresenter"){
        auto panel=r.read_object_field(presenter,"_rewardPanel");
        // This panel is owned separately from the FanPresent screen View.
        // Its native selecting phase owns the existing reward selector.
        if(!panel||r.field<bool>(presenter,"_isCompleted")||
            !r.field<bool>(panel,"_isSelecting")||r.field<bool>(panel,"_isReceiving"))return nullptr;
        auto panel_view=r.read_object_field(panel,"_view");
        if(!panel_view)return nullptr;
        candidates={r.read_object_field(panel_view,"_selectRewardView")};
    }else candidates = (kind == "ProduceRewardSelectorDialogPresenter" || kind == "ProduceChangeCardRewardSelectorDialogPresenter") ?
        std::vector<void*>{r.read_object_field(presenter, "_selectRewardView")} :
        children(r, view(r, presenter), "Assembly-CSharp.dll", "Campus.InGame", "ProduceSelectRewardView");
    for (void* candidate : candidates) {
        if (!candidate) continue;
        auto canvas = r.getter(candidate, "get_RootCanvasGroup");
        if (!active(r, candidate) || r.unbox<float>(r.getter(canvas, "get_alpha")) <= .001f) continue;
        if (selected) throw std::runtime_error("multiple active native reward selectors");
        selected = candidate;
    }
    return selected;
}
json card_data(Runtime& r, void* card) {
    if (!card) return nullptr;
    auto id = text(r, card, "get_Id");
    if (id.empty()) throw std::runtime_error("native card identity missing");
    // Card projections may be newly allocated on each getter call. Their
    // addresses must not enter revision/CAS; selector index or actual GUID /
    // UserProduceProgressProduceCard.Number provides the stable identity.
    json result = {{"card_id", id}, {"upgrade", integer(r, card, "get_UpgradeCount")},
                   {"evaluation", integer(r, card, "get_Evaluation")}};
    auto guid = optional_getter(r, card, "get_Guid");
    if (guid && !r.string(guid).empty()) result["card_guid"] = r.string(guid);
    result["customize_ids"] = json::array();
    if (auto ids = r.getter(card, "get_ProduceCardCustomizeIdList"))
        for (auto value : r.enumerate(ids, 256)) result["customize_ids"].push_back(r.string(value));
    result["customize_counts"] = integer_sequence(r, r.getter(card, "get_CustomizeCountList"));
    return result;
}
json reward_data(Runtime& r, void* reward) {
    json result = {{"resource_type", integer(r, reward, "get_ResourceType")},
                   {"resource_id", text(r, reward, "get_Id")},
                   {"quantity", integer(r, reward, "get_Quantity")}};
    auto card = r.getter(reward, "get_Card");
    if (card) result.update(card_data(r, card));
    return result;
}
json deck_card(Runtime& r, void* card) {
    if (!card) return nullptr;
    auto data = r.getter(card, "GetProduceCardData");
    auto result = card_data(r, data);
    result["deck_number"] = integer(r, card, "get_Number");
    result["deleted"] = flag(r, card, "get_Deleted");
    result["customizing"] = flag(r, card, "get_Customizing");
    return result;
}
void* item_cell(Runtime& r, void* list, int index, void* expected_model) {
    auto cell = r.invoke(r.method(r.object_class(list), "GetCellFromIndex", 1), list, {&index});
    if (!cell || !active(r, cell)) return nullptr;
    if (r.getter(cell, "GetItemModel") != expected_model)
        throw std::runtime_error("native recycled cell no longer binds the requested item model");
    return cell;
}
void* cell_button(Runtime& r, void* cell, bool resource = false) {
    auto cell_view = r.getter(cell, "get_View");
    return resource ? r.read_object_field(cell_view, "_produceRewardIconButton") : r.getter(cell_view, "get_Button");
}
void click_cell(Runtime& r, void* list, int index, void* model, bool resource = false) {
    auto cell = item_cell(r, list, index, model);
    if (!cell) throw std::runtime_error("native cell must be revealed before selection");
    click(r, cell_button(r, cell, resource));
}
bool is_card_selector(const std::string& name) {
    return exam_card_selector_create_phase(name)>=0;
}
std::string select_kind(int value) {
    static constexpr std::array<const char*, 15> names = {"None", "Upgrade", "Change", "Duplicate", "ChangeUpgrade",
        "DuplicateUpgrade", "Add", "Delete", "Hand", "DeckFirst", "DeckLast", "DeckRandom", "Grave", "Lost", "Move"};
    if (value < 0 || value >= static_cast<int>(names.size())) throw std::runtime_error("unknown native CardSelectType");
    return names[static_cast<std::size_t>(value)];
}
void set_surface(json& snapshot, const char* family, json state, json actions, bool complete = true) {
    snapshot["surface"] = family;
    snapshot["ui_state"] = std::move(state);
    snapshot["legal_actions"] = std::move(actions);
    snapshot["actions_complete"] = complete;
}
bool selectors(Runtime& r, void* p, const std::string& screen, json& snapshot) {
    const bool resource = screen == "ProduceResourceSelectorOverlayPresenter";
    if (!resource && !is_card_selector(screen)) return false;
    auto list = r.read_object_field(p, resource ? "_list" : "_cardList");
    auto models = r.enumerate(r.getter(list, "get_ItemModels"), 4096);
    const int minimum = r.field<int>(p, "_selectCountMin");
    const int maximum = r.field<int>(p, "_selectCountMax");
    const int count = integer(r, p, "get_SelectCount");
    const bool valid = flag(r, p, "IsValidCount");
    json state = {{"family", "card-selector"}, {"selector_type", screen},
        {"selection_type", select_kind(r.field<int>(p, "_cardSelectType"))},
        {"minimum", minimum}, {"maximum", maximum}, {"selected_count", count},
        {"valid_count", valid}, {"native_head_index", r.field<int>(list, "_currentStartIndex")},
        {"native_tail_index", r.field<int>(list, "_currentEndIndex")}, {"candidates", json::array()}};
    json actions = json::array();
    const bool enabled = !flag(r, p, "get_IsClosing") && !r.field<bool>(p, "_isSuccess");
    int observed_selected = 0;
    for (int index = 0; index < static_cast<int>(models.size()); ++index) {
        auto model = models[static_cast<std::size_t>(index)];
        auto data = r.getter(model, resource ? "get_Resource" : "get_Card");
        auto row = resource ? reward_data(r, data) : card_data(r, data);
        const bool selected = flag(r, model, "get_IsSelected");
        const bool restricted = flag(r, model, "get_IsRestrict");
        auto cell = item_cell(r, list, index, model);
        const bool realized = cell != nullptr;
        row.update({{"index", index}, {"selected", selected}, {"restricted", restricted}, {"realized", realized},
                    {"instance_key", instance_id(p) + ":" + std::to_string(index)},
                    {"identity_kind", "native-selector-index"}});
        observed_selected += selected ? 1 : 0;
        state["candidates"].push_back(row);
        if (!enabled || restricted || (selected && maximum <= 1)) continue;
        // The game's single-choice handler replaces the previous selection;
        // multi-choice handlers toggle until their own maximum is reached.
        if (!selected && maximum != 1 && count >= maximum) continue;
        json target = {{"index", index}, {"instance_key", row["instance_key"]},
                       {"selected_before", selected}, {"selector_type", screen}};
        for (const auto* key : {"card_id", "upgrade", "card_guid", "resource_id", "resource_type"})
            if (row.contains(key)) target[key] = row[key];
        if (!realized) actions.push_back(action("card_choice.reveal", target));
        else if (clickable(r, cell_button(r, cell, resource)))
            actions.push_back(action(selected ? "card_choice.deselect" : "card_choice.select", target));
    }
    if (observed_selected != count) throw std::runtime_error("native selector selected count differs from full item models");
    auto confirm = common_button(r, p, "get_ExecuteButton");
    state["confirm_enabled"] = enabled && valid && clickable(r, confirm);
    if (state["confirm_enabled"] == true) actions.push_back(action("card_choice.confirm", {{"selector_type", screen}, {"selected_count", count}}));
    set_surface(snapshot, "card_choice", state, actions, enabled);
    return true;
}
bool rewards(Runtime& r, void* p, json& snapshot) {
    auto reward = reward_view(r, p);
    if (!reward) return false;
    auto values = r.enumerate(r.read_object_field(reward, "_rewardList"), 256);
    auto icons = r.enumerate(r.read_object_field(reward, "_selectRewardIconlist"), 256);
    if (values.size() > icons.size()) throw std::runtime_error("native reward list/icon count mismatch");
    auto canvas = r.getter(reward, "get_RootCanvasGroup");
    const bool enabled = flag(r, canvas, "get_interactable") && flag(r, canvas, "get_blocksRaycasts");
    const int selected = integer(r, reward, "get_SelectRewardIndex");
    const int status = integer(r, reward, "get_SelectStatus");
    json state = {{"family", "reward"}, {"selected_index", selected}, {"select_status", status},
                  {"candidates", json::array()}};
    json actions = json::array();
    for (int index = 0; index < static_cast<int>(values.size()); ++index) {
        auto row = reward_data(r, values[static_cast<std::size_t>(index)]);
        row.update({{"index", index}, {"selected", status == 1 && index == selected},
                    {"instance_key", instance_id(reward) + ":" + std::to_string(index)}});
        state["candidates"].push_back(row);
        if (enabled && clickable(r, icons[static_cast<std::size_t>(index)]) && !(status == 1 && index == selected)) {
            json target = {{"index", index}, {"instance_key", row["instance_key"]},
                           {"resource_id", row["resource_id"]}, {"resource_type", row["resource_type"]}};
            if (row.contains("card_id")) target.update({{"card_id", row["card_id"]}, {"upgrade", row["upgrade"]}});
            actions.push_back(action("reward.select", target));
        }
    }
    auto receive = r.read_object_field(reward, "_receiveButton");
    state["receive_enabled"] = enabled && status == 1 && selected >= 0 && selected < static_cast<int>(values.size()) && clickable(r, receive);
    if (state["receive_enabled"] == true) {
        const auto& row = state["candidates"][selected];
        actions.push_back(action("reward.receive", {{"index", selected}, {"instance_key", row["instance_key"]},
                                                    {"resource_id", row["resource_id"]}}));
    }
    set_surface(snapshot, "card_reward", state, actions, enabled);
    return true;
}
json reward_group_state(Runtime& r,void* presenter){
    auto panel=r.read_object_field(presenter,"_rewardPanel");
    if(!panel)return nullptr;
    auto panel_view=r.read_object_field(panel,"_view");
    auto button=panel_view?r.getter(panel_view,"get_RewardGroupOpenButton"):nullptr;
    auto groups=r.read_object_field(panel,"_presentRewardList");
    json state={{"owner_type",r.class_name(r.object_class(presenter))},
        {"owner_instance_id",instance_id(presenter)},{"panel_instance_id",instance_id(panel)},
        {"button_instance_id",instance_id(button)},
        {"group_index",r.field<int>(panel,"_groupIndex")},
        {"group_count",groups?json(r.enumerate(groups,256).size()):json(nullptr)},
        {"is_completed",r.field<bool>(presenter,"_isCompleted")},
        {"is_receiving",r.field<bool>(panel,"_isReceiving")},
        {"is_selecting",r.field<bool>(panel,"_isSelecting")},
        {"is_waiting_open",r.field<bool>(panel,"_isWaitingOpen")},
        {"button",{{"active",active(r,button)},{"enabled",button&&flag(r,button,"get_IsEnabled")},
            {"disabled",!button||flag(r,button,"get_IsDisabled")},
            {"has_callback",button&&r.read_object_field(button,"onClickedCallback")!=nullptr}}}};
    state["ready"]=!native_reward_group_target(state).is_null();
    return state;
}
bool fan_present_rewards(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    if(screen!="ScheduleFanPresentScreenPresenter")return false;
    const auto state=reward_group_state(r,presenter);
    const auto target=state.is_null()?json(nullptr):native_reward_group_target(state);
    if(rewards(r,presenter,snapshot)){
        snapshot["ui_state"]["reward_group"]=state;
        return true;
    }
    // Opening/receiving/final completion are owned by the native panel. The
    // existing global effect reader can override this with effect.advance.
    json actions=json::array();
    if(!target.is_null())actions.push_back(action("reward.open_group",target));
    set_surface(snapshot,target.is_null()?"effect_resolution":"card_reward",
        {{"family","reward_group"},{"reward_group",state},{"candidates",json::array()},
         {"automatic_transition",target.is_null()},
         {"phase",target.is_null()?"fan_present_reward_transition":"await_reward_group_open"}},
        actions,snapshot.at("blockers").empty());
    return true;
}
bool reward_guide(Runtime& r, void* p, const std::string& screen, json& snapshot) {
    if (screen != "ProduceDeckGuideRecommendConfirmSheetPresenter") return false;
    const bool enabled = !flag(r, p, "get_IsClosing") && !flag(r, p, "get_IsDisableInteraction");
    json actions = json::array();
    if (enabled) {
        auto execute = common_button(r, p, "get_ExecuteButton");
        auto cancel = common_button(r, p, "get_CancelButton");
        if (clickable(r, execute)) actions.push_back(action("reward.acknowledge_guide", {{"button_source", "execute"}}));
        else if (clickable(r, cancel)) actions.push_back(action("reward.acknowledge_guide", {{"button_source", "cancel"}}));
    }
    set_surface(snapshot, "card_reward", {{"family", "reward-confirmation"}, {"candidates", json::array()},
                                         {"notice", "existing-deck-guide-preference; no checkbox modification"}}, actions, enabled);
    return true;
}
void* selected_customize_card(Runtime& r, void* model, bool overlay) {
    auto card = r.getter(model, overlay ? "get_SelectedCard" : "get_SelectCard");
    if(!overlay)return card;
    auto info=r.getter(card,"get_Value");
    return info?r.getter(info,"get_UserCard"):nullptr;
}
void bind_customization_capacity(Runtime& r,void* card,json& row){
    // This is the game's pure per-card display model, not a request or a
    // controller. Runtime pins the temporary managed object for this read.
    auto type=r.klass("Assembly-CSharp.dll","Campus.InGame.Schedule","CustomizeCardInfo");
    auto info=r.new_object(type);
    r.invoke(r.method(type,".ctor",1),info,{card});
    if(integer(r,info,"get_Number")!=row.at("deck_number").get<int>()||
       text(r,info,"get_ProduceCardId")!=row.at("card_id").get<std::string>())
        throw std::runtime_error("native customization capacity card identity changed");
    row.update({{"current_customize_count",integer(r,info,"get_CurrentCustomizeCount")},
                {"max_customize_count",integer(r,info,"get_MaxCustomizeCount")},
                {"card_customizations_remaining",integer(r,info,"GetRemainCustomizeCount")},
                {"customize_locked",flag(r,info,"get_IsLockedCustomizeIdolSkillCard")},
                {"can_customize",flag(r,card,"CanCustomize")},
                {"customization_capacity_source","native CustomizeCardInfo.GetRemainCustomizeCount"}});
}
bool customize(Runtime& r, void* p, const std::string& screen, json& snapshot) {
    const bool overlay = screen == "ProduceCustomizeCardSelectorOverlayPresenter";
    if (!overlay && screen != "ScheduleCustomizeScreenPresenter") return false;
    auto model = r.read_object_field(p, "_model");
    auto pview = view(r, p);
    auto list = r.read_object_field(p, "_cardList");
    auto models = r.enumerate(r.getter(list, "get_ItemModels"), 4096);
    auto selected = selected_customize_card(r, model, overlay);
    const int view_state = overlay ? integer(r, r.getter(model, "get_ViewState"), "get_Value") : integer(r, pview, "get_CustomizeType");
    const bool selecting_card = view_state == (overlay ? 1 : 0);
    const bool selecting_customize = view_state == (overlay ? 2 : 1);
    json state = {{"family", "customize"}, {"selector_type", screen},
                  {"view_state", view_state}, {"selecting_card", selecting_card}, {"selecting_customize", selecting_customize},
                  {"native_head_index", r.field<int>(list, "_currentStartIndex")},
                  {"native_tail_index", r.field<int>(list, "_currentEndIndex")},
                  {"selected_card", deck_card(r, selected)}, {"candidates", json::array()},
                  {"customizes", json::array()}};
    json actions = json::array();
    for (int index = 0; index < static_cast<int>(models.size()); ++index) {
        auto item = models[static_cast<std::size_t>(index)];
        auto card = r.getter(item, "get_Card");
        auto row = deck_card(r, card);
        auto cell = item_cell(r, list, index, item);
        row.update({{"index", index}, {"selected", flag(r, item, "get_IsSelected")},
                    {"remaining_count", integer(r, item, "get_RemainCount")},
                    {"lack_cost", flag(r, item, "get_IsLackCost")}, {"realized", cell != nullptr}});
        bind_customization_capacity(r,card,row);
        state["candidates"].push_back(row);
        if (!selecting_card || row["selected"] == true || row["deleted"] == true || row["lack_cost"] == true) continue;
        json target = {{"index", index}, {"deck_number", row["deck_number"]}, {"card_id", row["card_id"]},
                       {"upgrade", row["upgrade"]}, {"selector_type", screen}};
        if (!cell) actions.push_back(action("customize.reveal_card", target));
        else if (clickable(r, cell_button(r, cell))) actions.push_back(action("customize.select_card", target));
    }
    auto buttons = overlay ? r.enumerate(r.read_object_field(pview, "_customizeInfoButtons"), 128) :
                             r.enumerate(r.getter(pview, "get_CustomizeInfoButtonViews"), 128);
    const auto cached_id=overlay?std::string():text(r,model,"get_CurrentCustomizeId");
    int actual_selected_index=-1;
    json actual_selected_id=nullptr,actual_selected_count=nullptr;
    for (int index = 0; index < static_cast<int>(buttons.size()); ++index) {
        auto button = buttons[static_cast<std::size_t>(index)];
        if (!button || !active(r, button)) continue;
        auto data = r.getter(button, "get_Customize");
        if (!data) continue;
        auto cursor=r.read_object_field(button,"_selectCursor");
        auto cursor_root=cursor?r.read_object_field(cursor,"_cursorRoot"):nullptr;
        const bool is_selected=cursor_root&&flag(r,cursor_root,"get_activeInHierarchy");
        auto row = json::parse(text(r, data, "ToString"));
        row.update({{"index", index}, {"customize_id", text(r, data, "get_Id")},
                    {"customize_count", integer(r, data, "get_CustomizeCount")},
                    {"enabled", clickable(r, r.getter(button, "get_Button"))}, {"produce_points", integer(r, data, "get_ProducePoint")}});
        row["selected"]=is_selected;
        row["selection_noop"]=!overlay&&cached_id==row.at("customize_id").get<std::string>();
        if(!overlay&&is_selected){actual_selected_index=index;actual_selected_id=row.at("customize_id");actual_selected_count=row.at("customize_count");}
        row["grow_effects"] = json::array();
        for (auto effect : r.enumerate(r.getter(data, "get_ProduceCardGrowEffects"), 128))
            row["grow_effects"].push_back(json::parse(text(r, effect, "ToString")));
        state["customizes"].push_back(row);
        if (selecting_customize && selected && row["enabled"] == true && row["selection_noop"] == false)
            actions.push_back(action("customize.select_option", {{"index", index}, {"selector_type", screen},
                {"deck_number", integer(r, selected, "get_Number")}, {"customize_id", row["customize_id"]},
                {"customize_count", row["customize_count"]}}));
    }
    if (overlay) {
        auto chosen = r.getter(r.getter(model, "get_SelectedCustomize"), "get_Value");
        state["remaining_customize_count"]=integer(r,r.getter(model,"get_RemainCustomizeCardCount"),"get_Value");
        state["selected_customize_id"] = chosen ? json(text(r, chosen, "get_Id")) : json(nullptr);
        state["selected_customize_count"] = chosen ? json(integer(r, chosen, "get_CustomizeCount")) : json(nullptr);
    } else {
        state["cached_customize_id"] = cached_id;
        state["cached_customize_index"] = integer(r, model, "get_SelectCustomizeIndex");
        state["selected_customize_id"] = actual_selected_id;
        state["selected_customize_index"] = actual_selected_index<0?json(nullptr):json(actual_selected_index);
        state["remaining_customize_count"] = integer(r, model, "get_RemainingCustomizeCount");
        state["selected_customize_count"] = actual_selected_count;
    }
    auto open = r.getter(pview, overlay ? "get_SelectCardButton" : "get_CustomizeButton");
    state["open_options_enabled"] = selecting_card && selected && clickable(r, open);
    if (state["open_options_enabled"] == true)
        actions.push_back(action("customize.open_options", {{"selector_type", screen}, {"deck_number", integer(r, selected, "get_Number")}}));
    auto back = r.getter(pview, "get_BackButton");
    state["back_to_cards_enabled"] = selecting_customize && clickable(r, back);
    if (state["back_to_cards_enabled"] == true)
        actions.push_back(action("customize.back_to_cards", {{"selector_type", screen}}));
    auto execute = r.getter(pview, overlay ? "get_ExecuteCustomizeButton" : "get_ExecuteButton");
    const bool option_selected = state["selected_customize_id"].is_string() && !state["selected_customize_id"].get<std::string>().empty();
    state["execute_enabled"] = selecting_customize && selected && option_selected && clickable(r, execute);
    if (state["execute_enabled"] == true)
        actions.push_back(action("customize.execute", {{"selector_type", screen},
            {"deck_number", integer(r, selected, "get_Number")}, {"customize_id", state["selected_customize_id"]}}));
    auto advance = r.getter(pview, overlay ? "get_EndButton" : "get_GoNextButton");
    state["finish_enabled"] = clickable(r, advance);
    if(!overlay){
        state["native_finish_enabled"]=state["finish_enabled"];
        state["finish_canvas"]=schedule_customize_finish_canvas(r,pview);
        state["finish_enabled"]=state["native_finish_enabled"]==true&&state["finish_canvas"].at("input_ready")==true;
    }
    if (state["finish_enabled"] == true) actions.push_back(action("customize.finish", {{"selector_type", screen}}));
    apply_customization_budget(state,actions);
    if(!overlay)apply_schedule_customize_start_gate(state,actions,schedule_customize_lifecycle(r));
    set_surface(snapshot, "card_customize", state, actions, selecting_card || selecting_customize);
    return true;
}
void bind_exam_continuation(Runtime& r, void* selector, json& snapshot) {
    if (snapshot.value("underlying_screen_type", "") != "ExamScreenPresenter" || snapshot.value("surface", "") != "card_choice") return;
    auto screen = active_screen(r);
    auto sequence = r.getter(screen, "get_Sequence");
    auto stack = r.getter(sequence, "get_CommandStack");
    auto queued = r.getter(stack, "get_CurrentTargetCommand");
    snapshot["exam_continuation"] = true;
    snapshot["ui_state"]["exam_continuation"] = true;
    if (!flag(r, sequence, "get_IsCommandPlaying")) {
        snapshot["actions_complete"] = false;
        snapshot["legal_actions"] = json::array();
        snapshot["ui_state"]["blocker"] = "exam-selector-has-no-playing-native-command";
        return;
    }
    ExamCloseSelectorOwner owner;
    try {
        owner=read_exam_close_selector_owner(r,selector,screen,sequence,r.getter(sequence,"get_Parameter"),
            snapshot.at("ui_state").at("minimum").get<int>(),snapshot.at("ui_state").at("maximum").get<int>());
    } catch(const std::exception& error) {
        snapshot["actions_complete"]=false;
        snapshot["legal_actions"]=json::array();
        snapshot["ui_state"]["blocker"]="exam-selector-active-owner-unproven";
        snapshot["ui_state"]["owner_error"]=error.what();
        try{snapshot["ui_state"]["owner_diagnostic"]=selector_owner_diagnostic(r,selector);}
        catch(const std::exception& diagnostic_error){snapshot["ui_state"]["owner_diagnostic_error"]=diagnostic_error.what();}
        return;
    }
    auto command=owner.command;
    json context = {{"sequence_id", instance_id(sequence)}, {"current_command_native_id", command ? json(instance_id(command)) : json(nullptr)},
                    {"is_command_playing",true},
                    {"queued_command_native_id",queued?json(instance_id(queued)):json(nullptr)},
                    {"selector_owner",{{"schema","gkms.exam-selector-close-owner.v1"},
                        {"selector_id",instance_id(selector)},{"callback_id",instance_id(owner.callback)},
                        {"completion_id",instance_id(owner.completion)},{"event_runner_id",instance_id(owner.event_runner)},
                        {"open_runner_id",instance_id(owner.open_runner)},{"create_runner_id",instance_id(owner.create_runner)},
                        {"select_runner_id",instance_id(owner.select_runner)},{"handler_runner_id",instance_id(owner.handler_runner)},
                        {"execute_runner_id",instance_id(owner.execute_runner)}}}};
    if (command) {
        auto card = r.getter(command, "get_PlayingCard");
        auto drink = r.read_object_field(command, "_playingDrink");
        context.update({{"source_card_guid", card ? json(text(r, card, "get_Guid")) : json(nullptr)},
                        {"source_drink_id", drink ? json(text(r, drink, "get_Id")) : json(nullptr)},
                        {"play_type", r.field<int>(command, "_playType")},
                        {"play_index", r.field<int>(command, "_playIndex")},
                        {"is_manual", r.field<bool>(command, "_isManual")},
                        {"is_card_select", r.field<bool>(command, "_isCardSelect")},
                        {"is_card_select2", r.field<bool>(command, "_isCardSelect2")},
                        {"card_search_id", r.string(r.read_object_field(command, "_cardSelectSearchId"))},
                         {"card_search_id2", r.string(r.read_object_field(command, "_cardSelectSearchId2"))}});
        if(context.at("is_manual")==false){
            auto parameter=r.getter(sequence,"get_Parameter");
            auto effect=r.read_object_field(command,"_playEffect");
            context["native_turn"]=integer(r,parameter,"get_CurrentTurn");
            context["is_replay"]=flag(r,parameter,"get_IsReplay");
            context["effect_id"]=effect?r.string(r.read_object_field(effect,"_id")):std::string();
            json logs=json::array();
            // This is a List<ExamPlayLog>, not a dictionary or an inferred
            // ancestor stack. The retained Command getter is the original ID.
            const auto native_logs=r.enumerate(r.getter(parameter,"get_PlayLogList"),8192);
            context["native_play_log_count"]=native_logs.size();
            for(auto log:native_logs){
                if(flag(r,log,"get_IsSelectLog"))continue;
                auto logged=r.getter(log,"get_Command");
                if(!logged)throw std::runtime_error("Exam PlayLog has no native command");
                auto logged_card=r.getter(logged,"get_PlayingCard");
                auto logged_drink=r.read_object_field(logged,"_playingDrink");
                logs.push_back({{"command_native_id",instance_id(logged)},
                    {"is_select_log",flag(r,log,"get_IsSelectLog")},{"is_cost_failed",flag(r,log,"get_IsCostFailed")},
                    {"is_manual",flag(r,logged,"get_IsManual")},{"turn",integer(r,log,"get_CurrentTurn")},
                    {"play_type",integer(r,logged,"get_PlayType")},{"play_index",integer(r,logged,"get_PlayIndex")},
                    {"source_card_guid",logged_card?json(text(r,logged_card,"get_Guid")):json(nullptr)},
                    {"source_drink_id",logged_drink?json(text(r,logged_drink,"get_Id")):json(nullptr)}});
            }
            context["manual_root"]=latest_manual_main_command(logs);
        }
    }
    snapshot["exam_continuation"] = true;
    snapshot["parent_context"] = context;
    try{snapshot["exam_model_observation"]=capture_secondary_model_observation(r,sequence,r.getter(sequence,"get_Parameter"),selector,owner,context,snapshot.at("ui_state"));}
    catch(const std::exception& error){snapshot["exam_model_observation"]={{"schema","gkms.live-exam-model-observation.v1"},
        {"decision_type","secondary"},{"complete",false},{"read_errors",json::array({error.what()})}};}
    snapshot["ui_state"]["exam_continuation"] = true;
    snapshot["ui_state"]["parent_context"] = context;
    for (auto& candidate : snapshot["legal_actions"]) {
        candidate["target"]["exam_continuation"] = true;
        candidate["target"]["parent_context"] = context;
    }
}
}

bool append_produce_card_ui_actions(Runtime& r, void* presenter, const std::string& screen, json& snapshot) {
    if (snapshot.at("busy") != false) return false;
    if(fan_present_rewards(r,presenter,screen,snapshot))return true;
    if(screen=="SimpleSheetPresenter"&&snapshot.value("underlying_screen_type",std::string())=="ScheduleCustomizeScreenPresenter"){
        auto parent=active_screen(r);
        auto model=r.read_object_field(parent,"_model");
        const int remaining=integer(r,model,"get_RemainingCustomizeCount");
        auto button=r.getter(presenter,"get_ExecuteButton");
        json actions=json::array();
        if(clickable(r,button))actions.push_back(action("customize.confirm_finish",{{"selector_type",screen},
            {"parent_instance_id",instance_id(parent)},{"remaining_customize_count",remaining},
            {"button_instance_id",instance_id(button)}}));
        set_surface(snapshot,"card_customize",{{"family","customize_finish_confirmation"},{"selector_type",screen},
            {"remaining_customize_count",remaining}},actions);
        return true;
    }
    if(screen=="ScheduleCustomizeExecuteSheetPresenter"){
        auto model=r.getter(presenter,"get_Model");
        auto card=r.getter(model,"get_Card");
        const auto customize_id=text(r,model,"get_CustomizeId");
        const int points=integer(r,model,"get_ProducePoint");
        const int number=integer(r,card,"get_Number");
        const auto card_id=text(r,card,"get_ProduceCardId");
        auto button=common_button(r,presenter,"get_ExecuteButton");
        json actions=json::array();
        if(clickable(r,button))actions.push_back(action("customize.confirm_execute",{{"selector_type",screen},
            {"deck_number",number},{"card_id",card_id},{"customize_id",customize_id},{"produce_points",points},
            {"button_instance_id",instance_id(button)}}));
        set_surface(snapshot,"card_customize",{{"family","customize_confirmation"},{"selector_type",screen},
            {"deck_number",number},{"card_id",card_id},{"customize_id",customize_id},{"produce_points",points}},actions);
        return true;
    }
    if (reward_guide(r, presenter, screen, snapshot)) return true;
    if (selectors(r, presenter, screen, snapshot) || customize(r, presenter, screen, snapshot)) {
        if(snapshot.value("surface",std::string())=="card_choice"){
            auto parent=active_screen(r);
            bind_card_selector_parent(snapshot,parent?r.class_name(r.object_class(parent)):std::string(),
                parent?instance_id(parent):std::string());
        }
        bind_exam_continuation(r, presenter, snapshot);
        return true;
    }
    return rewards(r, presenter, snapshot);
}

bool submit_produce_card_ui_action(Runtime& r, void* p, const json& target, const json&) {
    const auto name = target.at("action_id").get<std::string>();
    if (name.rfind("reward.", 0) == 0) {
        if(name=="reward.open_group"){
            if(r.class_name(r.object_class(p))!="ScheduleFanPresentScreenPresenter")
                throw std::runtime_error("native reward group owner changed");
            const auto state=reward_group_state(r,p);
            if(state.is_null()||native_reward_group_target(state)!=target)
                throw std::runtime_error("native reward group is no longer ready for the bound open action");
            auto panel=r.read_object_field(p,"_rewardPanel");
            click(r,r.getter(r.read_object_field(panel,"_view"),"get_RewardGroupOpenButton"));
            return true;
        }
        if (name == "reward.acknowledge_guide") {
            if (r.class_name(r.object_class(p)) != "ProduceDeckGuideRecommendConfirmSheetPresenter")
                throw std::runtime_error("native reward guide notice changed");
            click(r, common_button(r, p, target.at("button_source").get<std::string>() == "execute" ? "get_ExecuteButton" : "get_CancelButton"));
            return true;
        }
        auto reward = reward_view(r, p);
        if (!reward) throw std::runtime_error("native reward selector disappeared");
        if (name == "reward.select") {
            int index = target.at("index").get<int>();
            r.invoke(r.method(r.object_class(reward), "SelectReward", 1, 0x060017CB), reward, {&index});
        } else if (name == "reward.receive") click(r, r.read_object_field(reward, "_receiveButton"));
        else return false;
        return true;
    }
    const bool card = name.rfind("card_choice.", 0) == 0;
    const bool custom = name.rfind("customize.", 0) == 0;
    if (!card && !custom) return false;
    const auto screen = r.class_name(r.object_class(p));
    if (screen != target.at("selector_type").get<std::string>()) throw std::runtime_error("native card selector type changed");
    if(custom&&screen=="ScheduleCustomizeScreenPresenter"&&schedule_customize_lifecycle(r).at("input_ready")!=true)
        throw std::runtime_error("native Customize start is not complete or its step has already ended");
    if(card&&target.contains("parent_instance_id")&&!target.at("parent_instance_id").is_null()){
        auto parent=active_screen(r);
        if(!parent||instance_id(parent)!=target.at("parent_instance_id").get<std::string>()||
            r.class_name(r.object_class(parent))!=target.at("parent_screen_type").get<std::string>())
            throw std::runtime_error("native card selector underlying parent changed");
    }
    if(name=="customize.confirm_finish"){
        auto parent=active_screen(r);
        if(screen!="SimpleSheetPresenter"||r.class_name(r.object_class(parent))!="ScheduleCustomizeScreenPresenter"||
            instance_id(parent)!=target.at("parent_instance_id").get<std::string>())throw std::runtime_error("customize finish confirmation owner changed");
        auto button=r.getter(p,"get_ExecuteButton");
        if(instance_id(button)!=target.at("button_instance_id").get<std::string>())throw std::runtime_error("customize finish confirmation button changed");
        click(r,button);return true;
    }
    if(name=="customize.confirm_execute"){
        auto button=common_button(r,p,"get_ExecuteButton");
        if(instance_id(button)!=target.at("button_instance_id").get<std::string>())throw std::runtime_error("customize confirmation button changed");
        click(r,button);return true;
    }
    if (name == "card_choice.confirm") { click(r, common_button(r, p, "get_ExecuteButton")); return true; }
    if (name == "customize.open_options" || name == "customize.back_to_cards") {
        const bool overlay = screen == "ProduceCustomizeCardSelectorOverlayPresenter";
        click(r, r.getter(view(r, p), name == "customize.back_to_cards" ? "get_BackButton" :
            (overlay ? "get_SelectCardButton" : "get_CustomizeButton")));
        return true;
    }
    if (name == "customize.execute" || name == "customize.finish") {
        const bool overlay = screen == "ProduceCustomizeCardSelectorOverlayPresenter";
        if(name=="customize.finish"&&!overlay&&schedule_customize_finish_canvas(r,view(r,p)).at("input_ready")!=true)
            throw std::runtime_error("native Customize finish canvas is still hidden or not interactive");
        click(r, r.getter(view(r, p), name == "customize.execute" ?
            (overlay ? "get_ExecuteCustomizeButton" : "get_ExecuteButton") : (overlay ? "get_EndButton" : "get_GoNextButton")));
        return true;
    }
    int index = target.at("index").get<int>();
    if (name == "customize.select_option") {
        auto buttons = screen == "ProduceCustomizeCardSelectorOverlayPresenter" ?
            r.enumerate(r.read_object_field(view(r, p), "_customizeInfoButtons"), 128) :
            r.enumerate(r.getter(view(r, p), "get_CustomizeInfoButtonViews"), 128);
        click(r, r.getter(buttons.at(static_cast<std::size_t>(index)), "get_Button"));
        return true;
    }
    const bool resource = screen == "ProduceResourceSelectorOverlayPresenter";
    auto list = r.read_object_field(p, resource ? "_list" : "_cardList");
    if (name == "card_choice.reveal" || name == "customize.reveal_card") {
        r.invoke(r.method(r.object_class(list), "MoveScrollByHeadIndex", 1), list, {&index});
        return true;
    }
    auto models = r.enumerate(r.getter(list, "get_ItemModels"), 4096);
    if (index < 0 || static_cast<std::size_t>(index) >= models.size()) throw std::runtime_error("native selector index changed");
    if (name == "card_choice.select" || name == "card_choice.deselect" || name == "customize.select_card") {
        click_cell(r, list, index, models[static_cast<std::size_t>(index)], resource);
        return true;
    }
    return false;
}
}
