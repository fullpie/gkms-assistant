#include "inventory_adapter.hpp"
#include "screen_context.hpp"
#include "native_trace.hpp"
#include "pc_method_binding.hpp"
#include "memory_auto_adapter.hpp"
#include <algorithm>
#include <map>
#include <stdexcept>
#include <unordered_set>

namespace gkms::bridge {
namespace {
std::string text(Runtime& r, void* object, const char* property) {
    return r.string(r.getter(object, property));
}
int number(Runtime& r, void* object, const char* property) {
    return r.unbox<int>(r.getter(object, property));
}
std::string required_text(Runtime& r, void* object, const char* property) {
    auto value = text(r, object, property);
    if (value.empty()) throw std::runtime_error(std::string("inventory-empty:") + property);
    return value;
}
std::string enumeration(Runtime& r, void* object, const char* property,
                        const char* prefix) {
    void* value = r.getter(object, property);
    // Resolve on System.Enum: its instance is a boxed reference, unlike a
    // value-type override, whose runtime_invoke ABI would require unboxing.
    auto* enum_class = r.klass("mscorlib.dll", "System", "Enum");
    auto name = r.string(r.invoke(r.method(enum_class, "ToString", 0), value));
    if (name.empty()) throw std::runtime_error("inventory-enum-name-empty");
    return std::string(prefix) + name;
}
json parameters(Runtime& r, void* object) {
    return {
        {"vocal", number(r, object, "get_ProduceVocalStatusUp")},
        {"dance", number(r, object, "get_ProduceDanceStatusUp")},
        {"visual", number(r, object, "get_ProduceVisualStatusUp")},
        {"vocal_growth", number(r, object, "get_ProduceVocalGrowthRatePermil")},
        {"dance_growth", number(r, object, "get_ProduceDanceGrowthRatePermil")},
        {"visual_growth", number(r, object, "get_ProduceVisualGrowthRatePermil")},
        {"stamina", number(r, object, "get_ProduceStaminaStatusUp")},
    };
}
json inherited_card(Runtime& r, void* card) {
    if (!card) return nullptr; // An observed absence is not a fabricated card.
    json customizes = json::array();
    for (void* item : r.enumerate(r.getter(card, "get_Customizes"), 100)) {
        customizes.push_back({{"id", required_text(r, item, "get_Id")},
                             {"customizeCount", number(r, item, "get_CustomizeCount")}});
    }
    return {{"id", required_text(r, card, "get_Id")},
            {"upgradeCount", number(r, card, "get_UpgradeCount")},
            {"customizes", customizes}};
}
json idol(Runtime& r, void* value) {
    return {{"card_id", required_text(r, value, "get_IdolCardId")},
            {"level_limit_rank", number(r, value, "get_LevelLimitRank")},
            {"potential_rank", number(r, value, "get_PotentialRank")},
            {"prima_stella_upgraded_time",
             r.unbox<long long>(r.getter(value, "get_PrimaStellaUpgradedTime"))},
            {"produce_parameters", parameters(r, value)}};
}
json support(Runtime& r, void* value) {
    return {{"card_id", required_text(r, value, "get_SupportCardId")},
            {"level", number(r, value, "get_Level")},
            {"level_limit_rank", number(r, value, "get_LevelLimitRank")},
            {"stock_quantity", number(r, value, "get_StockQuantity")},
            {"plan_type", enumeration(r, value, "get_PlanType", "ProducePlanType_")},
            {"produce_parameters", parameters(r, value)}};
}
json memory(Runtime& r, void* value) {
    json abilities = json::array();
    for (void* item : r.enumerate(r.getter(value, "get_Abilities"), 100)) {
        abilities.push_back({{"id", required_text(r, item, "get_Id")},
                             {"level", number(r, item, "get_Level")}});
    }
    return {{"memory_id", required_text(r, value, "get_UserMemoryId")},
            {"idol_card_id", required_text(r, value, "get_IdolCardId")},
            {"plan_type", enumeration(r, value, "get_PlanType", "ProducePlanType_")},
            {"produce_card", inherited_card(r, r.getter(value, "get_ProduceCard"))},
            {"produce_card_phase_type", enumeration(r, value, "get_ProduceCardPhaseType",
                                                     "ProduceMemoryProduceCardPhaseType_")},
            {"abilities", abilities}};
}
struct CollectionRead { void* object; int count; json rows; };
CollectionRead collection(Runtime& r, void* manager, const char* getter,
                          const char* id, json (*project)(Runtime&, void*)) {
    void* object = r.getter(manager, getter);
    const int expected = number(r, object, "get_Count");
    if (expected < 0 || expected > 100000)
        throw std::runtime_error("inventory-collection-size-out-of-bounds");
    void* all = r.invoke(r.method(r.object_class(object), "GetAll", 0), object);
    const auto values = r.enumerate(all, 100000);
    if (values.size() != static_cast<size_t>(expected))
        throw std::runtime_error("inventory-collection-count-mismatch");
    json rows = json::array();
    std::unordered_set<std::string> identities;
    for (void* value : values) {
        if (!value) throw std::runtime_error("inventory-null-entry");
        auto row = project(r, value);
        if (!identities.insert(row.at(id).get<std::string>()).second)
            throw std::runtime_error("inventory-duplicate-instance");
        rows.push_back(std::move(row));
    }
    return {object, expected, std::move(rows)};
}
void* user_manager(Runtime& runtime) {
    auto* type = runtime.klass("Assembly-CSharp.dll", "Campus.Common.User", "UserDataManager");
    auto result = runtime.invoke(runtime.method(type, "get_Instance", 0, 0x06016C2A), nullptr);
    if (!result) throw std::runtime_error("inventory-user-manager-unavailable");
    return result;
}
void* current_select_info(Runtime& runtime,bool require_idle=false) {
    const auto context = screen_context_snapshot(runtime);
    if ((require_idle&&context.at("busy") != false) || context.at("active") != true)
        throw std::runtime_error("loadout-active-screen-busy-or-inactive");
    auto screen = active_screen(runtime);
    const auto name = runtime.class_name(runtime.object_class(screen));
    if (name != "ProduceSupportCardSelectScreenPresenter" && name != "ProduceMemorySelectScreenPresenter")
        throw std::runtime_error("loadout-requires-active-support-or-memory-select-screen");
    auto model = runtime.read_object_field(screen, "_model");
    auto info = runtime.read_object_field(model, "_selectInfo");
    if (!info) throw std::runtime_error("loadout-active-selection-info-unavailable");
    return info;
}
std::string rental_key(Runtime& runtime, void* support_card) {
    const auto card = required_text(runtime, support_card, "get_ResourceId");
    const auto owner = text(runtime, support_card, "get_PublicUserId");
    const auto level = number(runtime, support_card, "get_Level");
    const auto rank = number(runtime, support_card, "get_LevelLimitRankInt");
    return sha256(json::array({owner, card, level, rank}).dump());
}
int support_rental_position(Runtime& runtime,void* edit){
    const int existing=number(runtime,edit,"get_RentalResourcePosition");
    if(existing>=0){trace_mark("loadout.rental_position",{{"existing",existing},{"source","existing-rental"}});return existing;}
    int slot_count=6;
    const int empty=runtime.unbox<int>(runtime.invoke(runtime.method(runtime.object_class(edit),"GetFirstEmptyPosition",1),edit,{&slot_count}));
    trace_mark("loadout.rental_position",{{"existing",existing},{"first_empty",empty},{"slot_count",slot_count},{"source","game-GetFirstEmptyPosition"}});
    return empty;
}
json current_resources(Runtime& runtime, void* edit, bool is_memory,std::string* state=nullptr) {
    trace_mark("loadout.deck.before",{{"kind",is_memory?"memory":"support"},{"edit_present",edit!=nullptr}});
    if(!edit){if(state)*state="not-initialized";trace_mark("loadout.edit.not_initialized",{{"kind",is_memory?"memory":"support"}});return json::array();}
    auto deck = runtime.getter(edit, "get_CurrentDeck");
    json rows = json::array();
    if(!deck){if(state)*state="not-initialized";return rows;}
    auto resources=runtime.getter(deck,"get_Resources");
    if(!resources){if(state)*state="not-initialized";return rows;}
    for (void* resource : runtime.enumerate(resources, 16)) {
        if (!resource) { rows.push_back(nullptr); continue; }
        auto row = json{{"position", number(runtime, resource, "get_Position")},
                        {is_memory ? "memory_id" : "card_id", required_text(runtime, resource, "get_ResourceId")},
                        {"is_rental", runtime.unbox<bool>(runtime.getter(resource, "get_IsRental"))}};
        if (!is_memory) {
            row["level"] = number(runtime, resource, "get_Level");
            if (row["is_rental"] == true) row["rental_key"] = rental_key(runtime, resource);
        }
        rows.push_back(std::move(row));
    }
    if(state)*state=rows.empty()?"empty":"ready";
    trace_mark("loadout.deck.after",{{"kind",is_memory?"memory":"support"},{"count",rows.size()}});
    return rows;
}
std::map<std::string, void*> owned_by_id(Runtime& runtime, void* manager, const char* getter,
                                      const char* identity_getter) {
    auto object = runtime.getter(manager, getter);
    auto all = runtime.invoke(runtime.method(runtime.object_class(object), "GetAll", 0), object);
    std::map<std::string, void*> result;
    for (void* value : runtime.enumerate(all, 100000)) {
        if (!result.emplace(required_text(runtime, value, identity_getter), value).second)
            throw std::runtime_error("loadout-duplicate-owned-identity");
    }
    return result;
}
}

json read_memory_resources(Runtime& runtime,void* edit) {
    return current_resources(runtime,edit,true);
}

json read_inventory(Runtime& runtime) {
    void* manager = user_manager(runtime);
    // Read the current transaction User, not the persisted LocalSaveUserData:
    // the latter can still identify the previous account during login.
    void* user = runtime.getter(manager, "get_User");
    const auto owner = required_text(runtime, user, "get_PublicUserId");
    auto idols = collection(runtime, manager, "get_UserIdolCardList", "card_id", idol);
    auto supports = collection(runtime, manager, "get_UserSupportCardList", "card_id", support);
    auto memories = collection(runtime, manager, "get_UserMemoryList", "memory_id", memory);
    for (const auto* observed : {&idols, &supports, &memories}) {
        if (number(runtime, observed->object, "get_Count") != observed->count)
            throw std::runtime_error("inventory-changed-during-read");
    }
    if (required_text(runtime, runtime.getter(manager, "get_User"), "get_PublicUserId") != owner)
        throw std::runtime_error("inventory-account-changed-during-read");
    const auto captured = utc_now();
    json result = {{"schema", "gkms.account-inventory.v1"},
            {"source", "dll-user-data-manager"},
            {"account_scope", "sha256:" + sha256(owner)},
            {"captured_at", captured},
            {"game_version", std::string("metadata-sha256:") + verified_pc_binding_identity().at("metadata_sha256").get<std::string>()},
            {"master_version", nullptr},
            {"complete", true},
            {"collection_counts", {{"idol_cards", idols.count},
                                   {"support_cards", supports.count},
                                   {"memories", memories.count}}},
            {"idol_cards", std::move(idols.rows)},
            {"support_cards", std::move(supports.rows)},
            {"memories", std::move(memories.rows)}};
    for (auto [name, key] : {std::pair{"idol_cards", "card_id"},
                              {"support_cards", "card_id"}, {"memories", "memory_id"}})
        std::sort(result[name].begin(), result[name].end(), [key](const json& a, const json& b) {
            return a.at(key).get<std::string>() < b.at(key).get<std::string>();
        });
    auto content = result;
    content.erase("captured_at");
    result["revision"] = sha256(content.dump());
    return result;
}

void initialize_inventory(Runtime& runtime) {
    // Active context comes from the shared WindowNode/TopScreen observer.
    // Resolve the current-PC selection contract without another hook owner.
    auto type = runtime.klass("Assembly-CSharp.dll", "Campus.OutGame", "ProduceSelectInfo");
    runtime.method(type, "get_SupportCardDeckEditInfo", 0, 0x0600E458);
    runtime.method(type, "get_MemoryDeckEditInfo", 0, 0x0600E45A);
    runtime.method(type, "SetUserIdolCard", 2, 0x0600E477);
}

json read_loadout(Runtime& runtime) {
    auto info = current_select_info(runtime);
    auto support_edit = runtime.getter(info, "get_SupportCardDeckEditInfo");
    auto memory_edit = runtime.getter(info, "get_MemoryDeckEditInfo");
    auto screen=active_screen(runtime);
    const auto screen_type=runtime.class_name(runtime.object_class(screen));
    const auto active_section=screen_type=="ProduceSupportCardSelectScreenPresenter"?"support":"memory";
    trace_mark("loadout.edit_info",{{"support_present",support_edit!=nullptr},{"memory_present",memory_edit!=nullptr},{"active_section",active_section}});
    json rentals = json::array();
    trace_mark("loadout.rentals.before");
    auto rental_collection=runtime.getter(support_edit,"get_RentalCards");
    const bool rental_enabled=runtime.unbox<bool>(runtime.getter(support_edit,"get_IsRentalEnabled"));
    const auto rental_values=rental_collection?runtime.enumerate(rental_collection,10000):std::vector<void*>{};
    for (void* card : rental_values) {
        if (!card) throw std::runtime_error("loadout-null-rental");
        rentals.push_back({{"rental_key", rental_key(runtime, card)},
                           {"card_id", required_text(runtime, card, "get_ResourceId")},
                           {"level", number(runtime, card, "get_Level")},
                           {"plan_type", enumeration(runtime, card, "get_PlanType", "ProducePlanType_")},
                           {"produce_parameters", parameters(runtime, card)}});
    }
    trace_mark("loadout.rentals.after",{{"loaded",rental_collection!=nullptr},{"count",rentals.size()}});
    std::string support_state,memory_state;
    auto support_cards=current_resources(runtime,support_edit,false,&support_state);
    auto memories=current_resources(runtime,memory_edit,true,&memory_state);
    auto user = runtime.getter(user_manager(runtime), "get_User");
    auto view=runtime.read_object_field(screen,"_view");
    auto enter=runtime.getter(view,"get_EnterButton");
    const auto context=screen_context_snapshot(runtime);
    const bool enter_enabled=enter&&runtime.unbox<bool>(runtime.getter(enter,"get_IsEnabled"))&&
        !runtime.unbox<bool>(runtime.getter(enter,"get_IsDisabled"))&&context.at("busy")==false;
    json validation={{"enter_enabled",enter_enabled},{"reason",enter_enabled?"game-enter-enabled":"game-enter-disabled"}};
    if(support_edit){
        int plan=number(runtime,runtime.getter(info,"get_UserIdolCard"),"get_PlanType");
        validation["support_resource_count_valid"]=runtime.unbox<bool>(runtime.getter(support_edit,"IsValidResourceCount"));
        validation["support_ids_valid"]=runtime.unbox<bool>(runtime.getter(support_edit,"IsValidSupportCardId"));
        validation["support_plan_type_valid"]=runtime.unbox<bool>(runtime.invoke(runtime.method(runtime.object_class(support_edit),"IsValidPlanType",1),support_edit,{&plan}));
        validation["support_deck_valid"]=runtime.unbox<bool>(runtime.invoke(runtime.method(runtime.object_class(support_edit),"IsValidDeck",1),support_edit,{&plan}));
        validation["existing_rental_position"]=number(runtime,support_edit,"get_RentalResourcePosition");
        int support_slots=6;
        validation["first_empty_support_position"]=runtime.unbox<int>(runtime.invoke(runtime.method(runtime.object_class(support_edit),"GetFirstEmptyPosition",1),support_edit,{&support_slots}));
    }
    json result = {{"schema", "gkms.account-loadout.v1"},
        {"active_section",active_section},{"game_validation",validation},
        {"account_scope", "sha256:" + sha256(required_text(runtime, user, "get_PublicUserId"))},
        {"produce_id", required_text(runtime, runtime.getter(info, "get_Produce"), "get_Id")},
        {"idol_card_id", required_text(runtime, runtime.getter(info, "get_UserIdolCard"), "get_CardId")},
        {"support_cards",support_cards},{"support_cards_state",support_state},
        {"memories",memories},{"memories_state",memory_state},
        {"rental_enabled",rental_enabled},{"rental_cards_loaded",rental_collection!=nullptr},
        {"rental_cards_state",rental_collection?(rentals.empty()?"empty":"ready"):"not-loaded"},
        {"rental_support_cards", rentals}};
    result["memory_auto"] = read_memory_auto(runtime);
    result["revision"] = sha256(result.dump());
    result["captured_at"] = utc_now();
    return result;
}

json apply_loadout(Runtime& runtime, const json& target, const std::string& expected_revision) {
    auto info=current_select_info(runtime,true);
    const auto before=read_loadout(runtime);
    const auto phase=before.at("active_section").get<std::string>();
    if(expected_revision.empty()||before.at("revision")!=expected_revision)throw std::runtime_error("loadout-revision-changed");
    if(target.contains("phase")&&target.at("phase")!=phase)throw std::runtime_error("loadout-phase-does-not-match-current-screen");
    if(target.at("account_scope")!=before.at("account_scope")||target.at("produce_id")!=before.at("produce_id")||
       target.at("idol_card_id")!=before.at("idol_card_id"))throw std::runtime_error("loadout-current-character-or-produce-mismatch");
    const auto inventory=read_inventory(runtime);
    if(target.at("inventory_revision")!=inventory.at("revision"))throw std::runtime_error("loadout-inventory-changed");
    auto screen=active_screen(runtime);
    auto editor=runtime.getter(info,phase=="support"?"get_SupportCardDeckEditInfo":"get_MemoryDeckEditInfo");
    if(!editor)throw std::runtime_error(phase+"-editor-not-initialized-by-game");
    auto manager=user_manager(runtime);
    std::vector<std::pair<int,void*>> staged;
    std::map<int,std::string> expected_supports,expected_memories;
    const bool memory_overrides=target.contains("memory_slot_overrides");
    json expected_memory_resources;
    if(memory_overrides){
        if(phase!="memory"||target.contains("memory_ids")||target.contains("support_card_ids")||target.contains("rental_key"))
            throw std::runtime_error("memory-auto-lock-target-is-not-memory-only");
        validate_memory_auto_override(runtime,target,before);
        expected_memory_resources=memory_auto_override_resources(before.at("memories"),target.at("memory_slot_overrides"));
    }
    int rental_position=-1;
    std::string wanted_rental;
    if(target.contains("support_card_ids")){
        const auto ids=target.at("support_card_ids").get<std::vector<std::string>>();
        if(ids.size()==5&&std::unordered_set<std::string>(ids.begin(),ids.end()).size()==5){
            auto support_edit=runtime.getter(info,"get_SupportCardDeckEditInfo");
            if(support_edit){
                rental_position=support_rental_position(runtime,support_edit);
                if(rental_position<0||rental_position>=6)throw std::runtime_error("game-rental-allocation-result="+std::to_string(rental_position));
                std::size_t next{};
                for(int position=0;position<6;++position)if(position!=rental_position)expected_supports.emplace(position,ids.at(next++));
            }
        }else if(phase=="support")throw std::runtime_error("support-phase-requires-five-distinct-owned-cards");
    }
    if(target.contains("memory_ids")){
        const auto ids=target.at("memory_ids").get<std::vector<std::string>>();
        if(ids.size()==4&&std::unordered_set<std::string>(ids.begin(),ids.end()).size()==4)
            for(int position=0;position<4;++position)expected_memories.emplace(position,ids.at(position));
        else if(phase=="memory")throw std::runtime_error("memory-phase-requires-four-distinct-owned-memories");
    }
    if(phase=="support"){
        if(expected_supports.size()!=5)throw std::runtime_error("support-phase-target-incomplete");
        wanted_rental=target.at("rental_key").get<std::string>();
        auto rentals=runtime.getter(editor,"get_RentalCards");
        if(!rentals)throw std::runtime_error("rental-cards-not-loaded");
        void* rented{};
        for(auto candidate:runtime.enumerate(rentals,10000)){
            if(candidate&&rental_key(runtime,candidate)==wanted_rental){
                if(rented)throw std::runtime_error("rental-identity-ambiguous");rented=candidate;
            }
        }
        if(!rented)throw std::runtime_error("rental-not-currently-offered");
        const auto rented_id=required_text(runtime,rented,"get_ResourceId");
        for(const auto& [position,id]:expected_supports)if(id==rented_id)throw std::runtime_error("rental-duplicates-owned-support");
        auto owned=owned_by_id(runtime,manager,"get_UserSupportCardList","get_SupportCardId");
        auto type=runtime.klass("Assembly-CSharp.dll","Campus.Common.Data","ProduceSupportCard");
        auto own_ctor=runtime.method(type,".ctor",2,0x0601937B);
        auto rent_ctor=runtime.method(type,".ctor",2,0x0601937C);
        for(int position=0;position<6;++position){
            auto resource=runtime.new_object(type);
            if(position==rental_position)runtime.invoke(rent_ctor,resource,{rented,&position});
            else runtime.invoke(own_ctor,resource,{owned.at(expected_supports.at(position)),&position});
            staged.emplace_back(position,resource);
        }
    }else{
        if(!memory_overrides&&expected_memories.size()!=4)throw std::runtime_error("memory-phase-target-incomplete");
        auto owned=owned_by_id(runtime,manager,"get_UserMemoryList","get_UserMemoryId");
        auto type=runtime.klass("Assembly-CSharp.dll","Campus.Common.Data","ProduceMemory");
        auto ctor=runtime.method(type,".ctor",2,0x06019363);
        if(memory_overrides)for(const auto& lock:target.at("memory_slot_overrides"))
            expected_memories.emplace(lock.at("position").get<int>(),lock.at("locked_memory_id").get<std::string>());
        // A partial lock constructs only its explicitly requested slots. All
        // other native resources, including rental memories, remain untouched.
        for(const auto& [slot,id]:expected_memories){
            int position=slot;
            auto resource=runtime.new_object(type);runtime.invoke(ctor,resource,{owned.at(id),&position});
            staged.emplace_back(position,resource);
        }
    }
    auto set_position=runtime.method(runtime.object_class(editor),"SetCurrentResourcePosition",1,0x0600E53E);
    auto set_resource=runtime.method(runtime.object_class(editor),"SetResource",2,0x0600E540);
    auto update_view=runtime.method(runtime.object_class(screen),"UpdateView",0);
    auto update_panel=runtime.method(runtime.object_class(screen),"UpdateDeckPanel",0);
    if(read_loadout(runtime).at("revision")!=before.at("revision"))throw std::runtime_error("loadout-changed-before-first-setter");
    if(memory_overrides)validate_memory_auto_override(runtime,target,before);
    int recommend_type=0;
    try{
        if(memory_overrides)mark_memory_auto_override_started(runtime);
        // Follow the active page's normal edit-model setters and presentation
        // refresh. Memory edit info is created by the game on the next page.
        for(auto [position,resource]:staged){
            runtime.invoke(set_position,editor,{&position});runtime.invoke(set_resource,editor,{resource,&recommend_type});
        }
        runtime.invoke(update_panel,screen);runtime.invoke(update_view,screen);
        const auto after=read_loadout(runtime);
        std::map<int,std::string> actual_supports,actual_memories;
        int rental_count{};bool rental_matches=false;
        for(const auto& row:after.at("support_cards")){
            if(row.is_null())continue;
            const int position=row.at("position").get<int>();
            if(row.at("is_rental")==true){
                ++rental_count;
                rental_matches=target.contains("rental_key")&&row.value("rental_key",std::string())==target.at("rental_key").get<std::string>()&&position==rental_position;
            }else actual_supports.emplace(position,row.at("card_id").get<std::string>());
        }
        for(const auto& row:after.at("memories"))if(!row.is_null()&&row.at("is_rental")==false)
            actual_memories.emplace(row.at("position").get<int>(),row.at("memory_id").get<std::string>());
        const bool supports_match=expected_supports.size()==5&&actual_supports==expected_supports&&rental_count==1&&rental_matches;
        const bool memories_match=memory_overrides?memory_auto_same_resources(after.at("memories"),expected_memory_resources):
            expected_memories.size()==4&&actual_memories==expected_memories;
        const bool matched=phase=="support"?supports_match:memories_match;
        json sections=json::array();if(supports_match)sections.push_back("support");if(memories_match)sections.push_back("memory");
        return {{"applied",matched},{"mutation_started",true},{"applied_sections",sections},
            {"full_loadout_applied",supports_match&&memories_match},
            {"reason",matched?"current-page-normal-setters-readback-matched":"current-page-readback-mismatch"},{"loadout",after}};
    }catch(const std::exception& error){
        json after=nullptr;try{after=read_loadout(runtime);}catch(...){}
        return {{"applied",false},{"mutation_started",true},{"applied_sections",json::array()},{"full_loadout_applied",false},
            {"reason",std::string("normal-page-setter-or-readback-failed:")+error.what()},{"loadout",after}};
    }catch(...){
        return {{"applied",false},{"mutation_started",true},{"applied_sections",json::array()},{"full_loadout_applied",false},
            {"reason","normal-page-setter-or-readback-failed:unknown-exception"},{"loadout",nullptr}};
    }
}
}
