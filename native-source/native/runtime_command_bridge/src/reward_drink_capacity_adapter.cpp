#include "reward_drink_capacity_adapter.hpp"
#include "pointer_identity.hpp"
#include "screen_context.hpp"
#include "native_layer_context.hpp"
#include <optional>

namespace gkms::bridge {
namespace {
bool flag(Runtime& r,void* o,const char* name){return r.unbox<bool>(r.getter(o,name));}
int number(Runtime& r,void* o,const char* name){return r.unbox<int>(r.getter(o,name));}
std::string text(Runtime& r,void* o,const char* name){return r.string(r.getter(o,name));}
bool active(Runtime& r,void* o){return o&&flag(r,r.getter(o,"get_gameObject"),"get_activeInHierarchy");}
bool clickable(Runtime& r,void* b){return active(r,b)&&flag(r,b,"get_IsEnabled")&&!flag(r,b,"get_IsDisabled")&&r.read_object_field(b,"onClickedCallback");}
bool canvas_ready(Runtime& r,void* c){return c&&active(r,c)&&flag(r,c,"get_interactable")&&flag(r,c,"get_blocksRaycasts")&&r.unbox<float>(r.getter(c,"get_alpha"))>.001f;}
bool ready(Runtime& r,void* o){
    if(!active(r,o))return false;
    const auto type=r.class_name(r.object_class(o));
    if(type=="ScheduleFanPresentScreenPresenter"){
        auto panel=r.read_object_field(o,"_rewardPanel");
        auto panel_view=panel?r.read_object_field(panel,"_view"):nullptr;
        auto select=panel_view?r.read_object_field(panel_view,"_selectRewardView"):nullptr;
        if(!panel||!select)return false;
        return reward_drink_capacity_panel_ready(active(r,panel),r.field<bool>(o,"_isCompleted"),
            r.field<bool>(panel,"_isReceiving"),r.field<bool>(panel,"_isSelecting"),
            canvas_ready(r,r.getter(select,"get_RootCanvasGroup")));
    }
    return reward_drink_capacity_owner_ready(type,true,flag(r,o,"get_IsClosing"),
        [&]{return !r.field<bool>(o,"_isSuccess")&&canvas_ready(r,r.getter(r.read_object_field(o,"_selectRewardView"),"get_RootCanvasGroup"));},
        [&]{return !flag(r,o,"get_IsDisableInteraction");});
}
void* common_button(Runtime& r,void* p,bool execute){return r.getter(r.getter(r.read_object_field(p,"_view"),"get_CommonView"),execute?"get_ExecuteButton":"get_CancelButton");}
void* screen_parent(Runtime& r,void* p){return r.invoke(r.method(r.object_class(p),"Campus.Common.IScreenLayer.GetParent",0,0x06015B4E),p);}
std::vector<void*> objects(Runtime& r,const char* ns,const char* name){
    auto type=r.klass("Assembly-CSharp",ns,name);
    auto object=r.klass("UnityEngine.CoreModule","UnityEngine","Object");
    return r.enumerate(r.invoke(r.method(object,"FindObjectsOfType",1,0x06001546),nullptr,{r.reflection_type(type)}),32);
}
std::vector<void*> invocations(Runtime& r,void* callback){return callback?r.enumerate(r.getter(callback,"GetInvocationList"),16):std::vector<void*>{};}
std::string callback_method(Runtime& r,void* callback){return text(r,r.getter(callback,"get_Method"),"get_Name");}
bool owns_reward_callback(Runtime& r,void* footer,void* reward,const char* method){
    int matches=0;
    for(auto callback:invocations(r,r.getter(footer,"get_OnChangeDrinkCallback")))
        if(r.getter(callback,"get_Target")==reward&&callback_method(r,callback)==method)++matches;
    return matches==1;
}
void* reward_footer(Runtime& r,void* reward,const char* method){
    void* found{};
    for(auto footer:objects(r,"Campus.InGame","ProduceFooterPresenter")){
        if(r.class_name(r.object_class(footer))!="ProduceFooterPresenter"||!active(r,footer)||!owns_reward_callback(r,footer,reward,method))continue;
        if(found)throw std::runtime_error("reward has multiple current footer owners");
        found=footer;
    }
    return found;
}
void* parent_reward(Runtime& r,void* transform){
    void* found{};
    for(auto p:objects(r,"Campus.InGame.Selector","ProduceRewardSelectorDialogPresenter")){
        if(!active(r,p))continue;
        auto view=r.read_object_field(p,"_selectRewardView");
        if(!view||r.getter(view,"get_transform")!=transform)continue;
        if(found)throw std::runtime_error("reward confirmation has multiple parent views");
        found=p;
    }
    auto screen=active_screen(r);
    if(screen&&r.class_name(r.object_class(screen))=="ScheduleFanPresentScreenPresenter"){
        auto panel=r.read_object_field(screen,"_rewardPanel");
        auto view=panel?r.read_object_field(panel,"_view"):nullptr;
        auto select=view?r.read_object_field(view,"_selectRewardView"):nullptr;
        if(select&&r.getter(select,"get_transform")==transform){
            if(found)throw std::runtime_error("reward confirmation has multiple bound owners");
            found=screen;
        }
    }
    return found;
}
struct Context {void* parent{};void* reward{};void* footer{};json state;};
std::optional<Context> read_context(Runtime& r,void* p){
    if(!p||!active(r,p))return {};
    const auto kind=r.class_name(r.object_class(p));
    void* panel{};void* view{};int group_index=-1,position=-1;
    if(kind=="ProduceRewardSelectorDialogPresenter"){
        if(flag(r,p,"get_IsClosing")||r.field<bool>(p,"_isSuccess"))return {};
        view=r.read_object_field(p,"_selectRewardView");
    }else if(kind=="ScheduleFanPresentScreenPresenter"){
        if(active_screen(r)!=p||r.field<bool>(p,"_isCompleted"))return {};
        panel=r.read_object_field(p,"_rewardPanel");
        if(!panel||!active(r,panel)||!r.field<bool>(panel,"_isSelecting")||r.field<bool>(panel,"_isReceiving"))return {};
        auto pv=r.read_object_field(panel,"_view");view=pv?r.read_object_field(pv,"_selectRewardView"):nullptr;
        const auto groups=r.enumerate(r.read_object_field(panel,"_presentRewardList"),256);
        group_index=r.field<int>(panel,"_groupIndex");
        if(group_index<0||group_index>=static_cast<int>(groups.size()))return {};
        auto group=groups[group_index];
        if(flag(r,group,"get_IsReceived")||number(r,group,"get_DisplayType")!=2||number(r,group,"get_ResourceType")!=3)return {};
        position=number(r,group,"get_PositionNumber");
    }else return {};
    if(!view||!active(r,view))return {};
    json rewards=json::array();
    for(auto reward:r.enumerate(r.read_object_field(view,"_rewardList"),256)){
        if(number(r,reward,"get_ResourceType")!=3)return {};
        rewards.push_back({{"index",rewards.size()},{"drink_id",text(r,reward,"get_Id")},{"quantity",number(r,reward,"get_Quantity")}});
    }
    if(rewards.empty())return {};
    auto user=r.klass("Assembly-CSharp","Campus.Common.User","UserDataManager");
    auto progress=r.invoke(r.method(user,"get_UserProduceProgress",0),nullptr);
    if(!progress)return {};
    if(panel){
        json presents=json::array();
        // UserDataManager returns its Collection wrapper, whose existing
        // GetAll() exposes the repeated entries (same contract as Outer).
        auto collection=r.invoke(r.method(user,"get_UserProduceProgressPresentList",0),nullptr);
        for(auto entry:r.enumerate(r.getter(collection,"GetAll"),256))
            presents.push_back(json::parse(text(r,entry,"ToString")));
        auto resolved=reward_panel_drink_quantities(presents,position,rewards);
        if(resolved.is_null())return {};
        rewards=std::move(resolved);
    }
    json owned=json::array();
    for(auto id:r.enumerate(r.getter(progress,"get_ProduceDrinkIds"),128)){
        const auto value=r.string(id);if(value.empty())throw std::runtime_error("owned native drink has no ID");
        owned.push_back({{"index",owned.size()},{"drink_id",value}});
    }
    const int limit=number(r,progress,"get_ProduceDrinkPossessLimit");
    const int selected=number(r,view,"get_SelectRewardIndex"),status=number(r,view,"get_SelectStatus");
    const char* callback=panel?"OnAfterDrinkRemove":"OnChangeDrink";
    auto footer=reward_footer(r,panel?panel:p,callback);
    json state={{"phase","reward_full"},{"all_rewards_are_drinks",true},{"is_drink_max",flag(r,progress,"get_IsDrinkMax")},
        {"select_status",status},{"parent_instance_id",pointer_identity(p)},{"reward_view_instance_id",pointer_identity(view)},
        {"selected_reward_index",selected},{"selected_reward_id",""},{"selected_reward_quantity",0},
        {"limit_count",limit},{"owned_drinks",owned},{"inventory_fingerprint",sha256(json::array({owned,limit}).dump())},
        {"reward_pool_fingerprint",sha256(rewards.dump())},{"footer_instance_id",footer?json(pointer_identity(footer)):json(nullptr)},
        {"parent_screen_type",kind},{"panel_instance_id",panel?json(pointer_identity(panel)):json(nullptr)},
        {"reward_group_index",group_index},{"reward_group_position_number",position},{"footer_callback_name",callback},
        {"source","native Progress slots + current owned reward view + exact footer callback"}};
    if(selected>=0&&selected<static_cast<int>(rewards.size())){
        state["selected_reward_id"]=rewards[selected]["drink_id"];
        state["selected_reward_quantity"]=rewards[selected]["quantity"];
        if(panel)state["selected_reward_ui_quantity"]=rewards[selected]["ui_quantity"];
    }
    return Context{p,view,footer,state};
}
json target_base(const Context& c){
    json out=json::object();
    for(const auto* key:{"parent_instance_id","reward_view_instance_id","selected_reward_index","selected_reward_id",
        "selected_reward_quantity","limit_count","inventory_fingerprint","footer_instance_id","reward_pool_fingerprint"})out[key]=c.state.at(key);
    for(const auto* key:{"parent_screen_type","panel_instance_id","reward_group_index","reward_group_position_number","footer_callback_name"})out[key]=c.state.at(key);
    return out;
}
json action(const char* id,json target){target["action_id"]=id;return {{"action_id",id},{"target",target}};}
void add_button(Runtime& r,const Context& c,void* owner,void* button,const char* id,json& actions,int slot=-1){
    if(!ready(r,owner)||!clickable(r,button))return;
    auto target=target_base(c);
    target["sheet_instance_id"]=pointer_identity(owner);target["button_instance_id"]=pointer_identity(button);
    target["callback_instance_id"]=pointer_identity(r.read_object_field(button,"onClickedCallback"));
    if(slot>=0){target["owned_index"]=slot;target["drink_id"]=c.state.at("owned_drinks").at(slot).at("drink_id");}
    actions.push_back(action(id,target));
}
// One continuation binding for an open that this DLL actually submitted. It
// contains weak handles, never stale raw pointers or an independent input loop.
struct Opened {GCHandle parent{},footer{},detail{},screen{},previous_layer{};json state;int slot{-1};std::string id;bool trash_submitted{};json layer_binding;};
std::optional<Opened> opened;
struct Skipped {GCHandle parent{},screen{},previous_layer{};json state;};
std::optional<Skipped> skipped;
void clear_opened(Runtime& r){if(opened){for(auto handle:{opened->parent,opened->footer,opened->detail,opened->screen,opened->previous_layer})if(handle)r.free_handle(handle);opened.reset();}}
void clear_skipped(Runtime& r){if(skipped){for(auto handle:{skipped->parent,skipped->screen,skipped->previous_layer})if(handle)r.free_handle(handle);skipped.reset();}}
bool child_binding(Runtime& r,void* child,void* original_screen,void* previous,void* requested_parent,json* diagnostic=nullptr){
    if(!child||!original_screen||active_screen(r)!=original_screen)return false;
    json binding;auto actual_previous=registered_layer_predecessor(r,child,&binding);
    auto parent=screen_parent(r,child);auto root=native_screen_sheet_root(r,original_screen);
    const bool bound=reward_capacity_child_parent_matches(active_layer(r)==child,
        reinterpret_cast<std::uintptr_t>(parent),reinterpret_cast<std::uintptr_t>(requested_parent),
        reinterpret_cast<std::uintptr_t>(root),reinterpret_cast<std::uintptr_t>(actual_previous),reinterpret_cast<std::uintptr_t>(previous));
    binding.update({{"actual_parent_transform_id",pointer_identity(parent)},{"requested_parent_transform_id",pointer_identity(requested_parent)},
        {"normalized_screen_root_id",pointer_identity(root)},{"expected_predecessor_id",pointer_identity(previous)},{"bound",bound},
        {"ownership_source","same submitted native callback + saved screen/previous layer + ordered active layer stack"}});
    if(diagnostic)*diagnostic=std::move(binding);
    return bound;
}
bool footer_slot(Runtime& r,const Context& c,int index,void*& button){
    if(!c.footer||!active(r,c.footer)||r.field<bool>(c.footer,"_isViewOnlyMode")||flag(r,c.footer,"get_IsOpenDialog"))return false;
    auto view=r.getter(c.footer,"get_View");
    if(!canvas_ready(r,r.read_object_field(view,"_drinkButtonCanvasGroup")))return false;
    const auto list=r.enumerate(r.getter(c.footer,"GetDrinkList"),128);
    const auto buttons=r.enumerate(r.getter(view,"get_DrinkButtonList"),128);
    if(list.size()!=c.state.at("owned_drinks").size()||index<0||index>=static_cast<int>(list.size())||index>=static_cast<int>(buttons.size()))return false;
    for(std::size_t i=0;i<list.size();++i)if(text(r,list[i],"get_Id")!=c.state.at("owned_drinks")[i].at("drink_id").get<std::string>())return false;
    auto wrapper=buttons[index];
    if(!active(r,wrapper)||flag(r,wrapper,"get_IsEmpty"))return false;
    button=r.getter(wrapper,"get_Button");if(!clickable(r,button))return false;
    const auto callbacks=invocations(r,r.read_object_field(button,"onClickedCallback"));
    if(callbacks.size()!=1||callback_method(r,callbacks[0])!="<UpdateDrinkButton>b__0")return false;
    auto closure=r.getter(callbacks[0],"get_Target");
    if(!closure||!r.has_field(r.object_class(closure),"i")||!r.has_field(r.object_class(closure),"<>4__this"))return false;
    return r.field<int>(closure,"i")==index&&r.read_object_field(closure,"<>4__this")==c.footer;
}
bool detail_matches(Runtime& r,void* detail,const Context& c,bool covered=false){
    if(!opened||!detail||r.class_name(r.object_class(detail))!="ProduceDrinkConfirmSheetPresenter"||
        !active(r,detail))return false;
    if(covered){
        if(!opened->detail||r.handle_target(opened->detail)!=detail||active_screen(r)!=r.handle_target(opened->screen))return false;
    }else if(!child_binding(r,detail,r.handle_target(opened->screen),
        opened->previous_layer?r.handle_target(opened->previous_layer):nullptr,r.getter(c.footer,"get_transform"),&opened->layer_binding))return false;
    // SetDrink's real trash callback carries this exact sheet and drink data.
    auto trash=r.getter(r.read_object_field(detail,"_view"),"get_TrashButton");
    const auto callbacks=invocations(r,r.read_object_field(trash,"onClickedCallback"));
    if(callbacks.size()!=1||callback_method(r,callbacks[0])!="<SetDrink>b__0")return false;
    auto closure=r.getter(callbacks[0],"get_Target");
    if(!closure||!r.has_field(r.object_class(closure),"<>4__this")||!r.has_field(r.object_class(closure),"drinkData"))return false;
    return r.read_object_field(closure,"<>4__this")==detail&&
        text(r,r.read_object_field(closure,"drinkData"),"get_Id")==opened->id&&
        !r.field<bool>(detail,"_canUse");
}
std::optional<Context> opened_context(Runtime& r){
    if(!opened)return {};
    auto c=read_context(r,r.handle_target(opened->parent));
    if(!c||!c->footer||c->footer!=r.handle_target(opened->footer)||
        !reward_drink_capacity_binding_matches(opened->state,c->state)||
        !reward_drink_capacity_slot_matches(c->state,opened->slot,opened->id))return {};
    return c;
}
void child_surface(json& snapshot,const Context& c,const char* phase,json actions){
    auto state=c.state;state["family"]="reward_drink_capacity";state["phase"]=phase;
    if(opened&&std::string(phase)!="confirm_skip"){
        state["opened_owned_index"]=opened->slot;state["opened_drink_id"]=opened->id;
        state["native_layer_parent_binding"]=opened->layer_binding;
    }
    snapshot["surface"]="reward_drink_capacity";snapshot["ui_state"]=state;snapshot["legal_actions"]=actions;
    snapshot["actions_complete"]=snapshot.at("blockers").empty();
}
bool project(Runtime& r,void* p,const std::string& screen,json& snapshot){
    if(screen=="ProduceRewardSelectorDialogPresenter"||screen=="ScheduleFanPresentScreenPresenter"){
        auto c=read_context(r,p);if(!c)return false;
        snapshot["ui_state"]["drink_capacity"]=c->state;
        if(!ready(r,p)||!canvas_ready(r,r.getter(c->reward,"get_RootCanvasGroup")))return true;
        const int status=c->state.at("select_status").get<int>();
        if(c->state.at("is_drink_max")==true&&status!=2)
            add_button(r,*c,p,r.read_object_field(c->reward,"_skipButton"),"reward.skip",snapshot["legal_actions"]);
        if(status==2)
            add_button(r,*c,p,r.read_object_field(c->reward,"_receiveButton"),"reward.confirm_skip",snapshot["legal_actions"]);
        if(!reward_drink_capacity_can_open(c->state))return true;
        for(int i=0;i<static_cast<int>(c->state.at("owned_drinks").size());++i){
            void* button{};
            if(footer_slot(r,*c,i,button))add_button(r,*c,p,button,"reward.drink_capacity_open",snapshot["legal_actions"],i);
        }
        return true;
    }
    if(screen=="ProducePresentSkipConfirmSheetPresenter"){
        auto p_reward=parent_reward(r,screen_parent(r,p));auto c=read_context(r,p_reward);
        json binding;
        if(!c&&skipped){
            c=read_context(r,r.handle_target(skipped->parent));
            if(!c||!reward_drink_capacity_same_identity(skipped->state,c->state)||
                !child_binding(r,p,r.handle_target(skipped->screen),skipped->previous_layer?r.handle_target(skipped->previous_layer):nullptr,
                    r.getter(c->reward,"get_transform"),&binding))c.reset();
        }
        if(!c||c->state.at("select_status")!=2)return false;
        json actions=json::array();
        add_button(r,*c,p,common_button(r,p,true),"reward.drink_capacity_confirm_skip",actions);
        add_button(r,*c,p,common_button(r,p,false),"reward.drink_capacity_cancel_skip",actions);
        child_surface(snapshot,*c,"confirm_skip",actions);
        if(!binding.is_null())snapshot["ui_state"]["native_layer_parent_binding"]=binding;
        return true;
    }
    if(screen!="ProduceDrinkConfirmSheetPresenter"&&screen!="SimpleSheetPresenter")return false;
    auto c=opened_context(r);
    if(!c){
        // Never leave a recognized stale capacity child to a generic sheet
        // confirm handler (the underlying event screen can still be present).
        auto footer=opened?r.handle_target(opened->footer):nullptr;
        auto detail=opened&&opened->detail?r.handle_target(opened->detail):nullptr;
        const bool owned_child=opened&&footer&&((screen=="ProduceDrinkConfirmSheetPresenter"&&
            child_binding(r,p,r.handle_target(opened->screen),opened->previous_layer?r.handle_target(opened->previous_layer):nullptr,
                r.getter(footer,"get_transform")))||(screen=="SimpleSheetPresenter"&&detail&&opened->trash_submitted&&
            child_binding(r,p,r.handle_target(opened->screen),detail,r.getter(r.read_object_field(detail,"_view"),"get_transform"))));
        if(!owned_child)return false;
        snapshot["surface"]="reward_drink_capacity";snapshot["legal_actions"]=json::array();snapshot["actions_complete"]=false;
        snapshot["ui_state"]={{"family","reward_drink_capacity"},{"phase","binding_changed"},
            {"binding_valid",false},{"reason","current reward, inventory or opened parent differs from submitted capacity open"}};
        return true;
    }
    if(screen=="ProduceDrinkConfirmSheetPresenter"){
        if(!detail_matches(r,p,*c))return false;
        if(opened->detail&&r.handle_target(opened->detail)!=p)return false;
        if(!opened->detail)opened->detail=r.weak_handle(p);
        json actions=json::array();
        add_button(r,*c,p,r.getter(r.read_object_field(p,"_view"),"get_TrashButton"),"reward.drink_capacity_trash",actions,opened->slot);
        add_button(r,*c,p,common_button(r,p,false),"reward.drink_capacity_cancel",actions,opened->slot);
        child_surface(snapshot,*c,"drink_detail",actions);return true;
    }
    auto detail=opened->detail?r.handle_target(opened->detail):nullptr;
    if(!opened->trash_submitted||!detail_matches(r,detail,*c,true)||
        !child_binding(r,p,r.handle_target(opened->screen),detail,r.getter(r.read_object_field(detail,"_view"),"get_transform"),&opened->layer_binding))return false;
    json actions=json::array();
    add_button(r,*c,p,common_button(r,p,true),"reward.drink_capacity_confirm",actions,opened->slot);
    add_button(r,*c,p,common_button(r,p,false),"reward.drink_capacity_cancel",actions,opened->slot);
    child_surface(snapshot,*c,"confirm_discard",actions);return true;
}
}
bool append_reward_drink_capacity_actions(Runtime& r,void* p,const std::string& screen,json& snapshot){
    if(snapshot.at("busy")!=false)return false;
    return project(r,p,screen,snapshot);
}
bool submit_reward_drink_capacity_action(Runtime& r,void* p,const json& target,const json& before){
    const auto id=target.value("action_id",std::string());
    if(id!="reward.skip"&&id!="reward.confirm_skip"&&id.rfind("reward.drink_capacity_",0)!=0)return false;
    // Reproject the real controls immediately before invoking a callback.
    json fresh=before;fresh["legal_actions"]=json::array();
    if(!project(r,p,r.class_name(r.object_class(p)),fresh))throw std::runtime_error("reward capacity owner no longer matches");
    bool found=false;
    for(const auto& action:fresh.at("legal_actions"))if(action.at("target")==target)found=true;
    if(!found)throw std::runtime_error("reward capacity target changed before callback");
    void* button{};
    if(id=="reward.drink_capacity_open"){
        auto c=read_context(r,p);const int slot=target.at("owned_index").get<int>();
        if(!c||!reward_drink_capacity_can_open(c->state)||!footer_slot(r,*c,slot,button))throw std::runtime_error("reward footer slot no longer available");
        clear_opened(r);
        auto original_screen=active_screen(r);auto previous=active_layer(r);
        if(!original_screen)throw std::runtime_error("reward footer open lacks original screen binding");
        opened=Opened{r.weak_handle(c->parent),r.weak_handle(c->footer),{},r.weak_handle(original_screen),
            previous?r.weak_handle(previous):GCHandle{},c->state,slot,target.at("drink_id").get<std::string>(),false,{}};
    }else if(id=="reward.skip"||id=="reward.confirm_skip"){
        auto c=read_context(r,p);
        button=r.read_object_field(c->reward,id=="reward.skip"?"_skipButton":"_receiveButton");
        if(id=="reward.confirm_skip"){
            clear_skipped(r);auto original_screen=active_screen(r);auto previous=active_layer(r);
            if(!original_screen)throw std::runtime_error("reward skip lacks original screen binding");
            skipped=Skipped{r.weak_handle(c->parent),r.weak_handle(original_screen),previous?r.weak_handle(previous):GCHandle{},c->state};
        }
    }else if(id=="reward.drink_capacity_trash")button=r.getter(r.read_object_field(p,"_view"),"get_TrashButton");
    else button=common_button(r,p,id=="reward.drink_capacity_confirm"||id=="reward.drink_capacity_confirm_skip");
    if(!clickable(r,button)||pointer_identity(button)!=target.at("button_instance_id").get<std::string>()||
        pointer_identity(r.read_object_field(button,"onClickedCallback"))!=target.at("callback_instance_id").get<std::string>())
        throw std::runtime_error("reward capacity callback no longer enabled or bound");
    if(id=="reward.drink_capacity_trash")opened->trash_submitted=true;
    if(id=="reward.drink_capacity_cancel"&&r.class_name(r.object_class(p))=="SimpleSheetPresenter")opened->trash_submitted=false;
    r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);
    return true;
}
}
