#include "live_loading_adapter.hpp"
#include "outer_pointer_guard.hpp"
#include "pointer_identity.hpp"
#include "pc_version_profile.hpp"
#include <array>

namespace gkms::bridge {
namespace {
void require(bool value,const char* message){if(!value)throw std::runtime_error(std::string("live loading: ")+message);}
bool type(Runtime& r,void* object,const char* ns,const char* name){
    return object&&r.class_namespace(r.object_class(object))==ns&&r.class_name(r.object_class(object))==name;
}
bool flag(Runtime& r,void* object,const char* getter){return r.unbox<bool>(r.getter(object,getter));}
bool active(Runtime& r,void* object){return object&&flag(r,r.getter(object,"get_gameObject"),"get_activeInHierarchy");}
std::string text(Runtime& r,void* object,const char* getter){const auto result=r.string(r.getter(object,getter));require(!result.empty(),"empty scope identity");return result;}
int status(Runtime& r,void* source){
    require(source&&r.object_class(source)==r.klass("UniTask.dll","Cysharp.Threading.Tasks","UniTaskCompletionSource"),"completion source type changed");
    auto value=r.getter(source,"UnsafeGetStatus");
    require(value&&r.object_class(value)==r.klass("UniTask.dll","Cysharp.Threading.Tasks","UniTaskStatus"),"completion status type changed");
    const int observed=r.unbox<int>(value);require(observed>=0&&observed<=3,"unknown completion status");return observed;
}
const char* status_name(int value){return std::array{"Pending","Succeeded","Faulted","Canceled"}.at(value);}
json canvas(Runtime& r,void* value){
    require(value!=nullptr,"loading canvas group unavailable");
    return {{"active",active(r,value)},{"alpha",r.unbox<float>(r.getter(value,"get_alpha"))},
        {"interactable",flag(r,value,"get_interactable")},{"blocks_raycasts",flag(r,value,"get_blocksRaycasts")}};
}
bool no_layer(Runtime& r){
    auto cls=r.klass("Assembly-CSharp","Campus.Common","ScreenLayerManager");
    if(!r.unbox<bool>(r.invoke(r.method(cls,"get_HasInstance",0,0x06000E83),nullptr)))return true;
    auto manager=r.invoke(r.method(cls,"get_Instance",0,0x06000E82),nullptr);
    return manager&&!flag(r,manager,"get_HasActive");
}
json scope(Runtime& r,void* live,void* fixed){
    require(type(r,live,"Campus.Live","LiveScenePresenter")&&active(r,live),"Live owner unavailable");
    require(type(r,fixed,"Campus.Live.Data","LiveFixedData"),"fixed Live data unavailable");
    auto model=r.read_object_field(live,"_liveModel");require(model&&r.getter(model,"get_FixedData")==fixed,"Live fixed-data owner changed");
    require(r.unbox<int>(r.getter(fixed,"get_LiveFromType"))==1,"only current Produce Live is supported");
    auto user=r.klass("Assembly-CSharp","Campus.Common.User","UserDataManager");
    auto progress=r.invoke(r.method(user,"get_UserProduceProgress",0),nullptr);
    require(progress&&flag(r,progress,"get_IsInProgress"),"current produce is not active");
    json value={{"produce_id",text(r,progress,"get_ProduceId")},
        {"idol_card_id",text(r,progress,"get_IdolCardId")},{"character_id",text(r,progress,"get_CharacterId")}};
    require(value.at("idol_card_id")==text(r,fixed,"get_IdolCardId")&&
        value.at("character_id")==text(r,fixed,"get_CharacterId"),"produce/Live identity differs");
    return value;
}
struct Waiter {void* closure{};void* source{};json observed;};
std::vector<Waiter> waiters(Runtime& r,void* button,void* callback){
    std::vector<Waiter> result;if(!callback)return result;
    auto base=r.klass("campus-submodule.Runtime.dll","Campus.Common","CampusButtonBase");
    auto closure_type=r.nested_class(base,"<>c__DisplayClass43_0");
    for(auto entry:r.enumerate(r.getter(callback,"GetInvocationList"),64)){
        auto method=r.getter(entry,"get_Method");
        if(!method||r.string(r.getter(method,"get_Name"))!="<WaitPressAsync>g__OnPressed|0")continue;
        auto closure=r.getter(entry,"get_Target");
        require(closure&&r.object_class(closure)==closure_type&&r.read_object_field(closure,"<>4__this")==button,
            "pressed waiter belongs to another closure/button");
        auto source=r.read_object_field(closure,"completionSource");const int observed=status(r,source);
        result.push_back({closure,source,{{"closure_id",pointer_identity(closure)},{"source_id",pointer_identity(source)},
            {"status",observed},{"status_name",status_name(observed)}}});
    }
    return result;
}
struct Press {std::array<GCHandle,6> roots{};json receipt=nullptr;bool terminal{};} last;
json last_press(Runtime& r){
    if(last.receipt.is_null()||last.terminal)return last.receipt;
    try{
        const int observed=status(r,r.handle_target(last.roots[0]));
        last.receipt["status"]=observed;last.receipt["status_name"]=status_name(observed);
        if(observed!=0){
            // The actual terminal status is immutable. Release the scene,
            // closure and UI roots; retain just this source identity until
            // the next admitted different source so its address cannot recycle.
            last.terminal=true;
            for(std::size_t i=1;i<last.roots.size();++i){r.free_handle(last.roots[i]);last.roots[i]=0;}
        }
    }catch(const std::exception& error){
        last.receipt["status"]=nullptr;last.receipt["status_name"]=nullptr;
        if(last.receipt["error"].is_null())last.receipt["error"]=error.what();
    }
    return last.receipt;
}
struct Observation {json state;void* button{};void* loading{};void* callback{};std::vector<Waiter> waits;};
Observation observe(Runtime& r,void* live,void* fixed){
    Observation out;out.state={{"schema","gkms.live-loading-observation.v1"},{"owner_bound",false},
        {"manager_exists",false},{"ready",false},{"target",nullptr},{"waiters",json::array()},
        {"last_press",last_press(r)},{"read_errors",json::array()}};
    if(!live||!fixed)return out;
    try{
        const auto identity=scope(r,live,fixed);out.state["scope"]=identity;
        out.state["live_presenter_id"]=pointer_identity(live);out.state["fixed_id"]=pointer_identity(fixed);
        auto cls=r.klass("Assembly-CSharp","Campus.Common","LoadingManager");
        // This getter does not create a singleton. Never call get_Instance if
        // the game's existing manager is absent.
        if(!r.unbox<bool>(r.invoke(r.method(cls,"get_ExistInstance",0),nullptr)))return out;
        auto manager=r.invoke(r.method(cls,"get_Instance",0),nullptr);require(type(r,manager,"Campus.Common","LoadingManager"),"loading manager type changed");
        out.state["manager_exists"]=true;out.state["manager_id"]=pointer_identity(manager);
        out.state["hiding"]=r.field<bool>(manager,"_isHiding");
        auto loading=r.read_object_field(manager,"_currentLoading");out.loading=loading;
        out.state["loading_id"]=pointer_identity(loading);
        out.state["loading_class"]=loading?json(r.class_namespace(r.object_class(loading))+"."+r.class_name(r.object_class(loading))):json(nullptr);
        if(!loading)return out;
        out.state["loading_type"]=r.unbox<int>(r.getter(loading,"get_LoadingType"));
        out.state["loading_active"]=active(r,loading);
        if(!type(r,loading,"Campus.InGame","LiveLoading"))return out;
        auto parameter=r.read_object_field(loading,"_currentParam");
        auto manager_parameter=r.read_object_field(manager,"_currentLoadingParam");
        out.state["param_id"]=pointer_identity(parameter);out.state["manager_param_id"]=pointer_identity(manager_parameter);
        out.state["owner_bound"]=parameter&&type(r,parameter,"Campus.InGame","LiveShowLoadingParam")&&
            manager_parameter==parameter&&r.read_object_field(parameter,"_liveFixedData")==fixed;
        auto now_loading=r.read_object_field(loading,"_nowLoadingRoot");
        out.state["now_loading_active"]=now_loading&&flag(r,now_loading,"get_activeInHierarchy");
        auto tap=r.read_object_field(loading,"_tapToStartRoot");out.state["tap_to_start_active"]=active(r,tap);
        out.state["canvas"]={{"root",canvas(r,r.read_object_field(loading,"_rootCanvasGroup"))},
            {"button_root",canvas(r,r.read_object_field(loading,"_buttonRoot"))}};
        auto button=r.read_object_field(loading,"_button");out.button=button;
        require(type(r,button,"Campus.Common","CampusButton"),"loading button type changed");
        auto callback=r.read_object_field(button,"onPressedCallback");out.callback=callback;
        out.state["button"]={{"active",active(r,button)},{"enabled",flag(r,button,"get_IsEnabled")},
            {"disabled",flag(r,button,"get_IsDisabled")},{"has_callback",callback!=nullptr},
            {"button_id",pointer_identity(button)},{"pressed_callback_id",pointer_identity(callback)}};
        out.waits=waiters(r,button,callback);for(const auto& waiter:out.waits)out.state["waiters"].push_back(waiter.observed);
        out.state["no_active_layer"]=no_layer(r);
        out.state["pointer_blocking"]=read_outer_pointer_guard(r,live,false);
        out.state["ready"]=live_loading_press_ready(out.state);
        if(out.state["ready"]==true){
            json target=identity;target["action_id"]="live.loading_continue";target["scope_fingerprint"]=sha256(identity.dump());
            for(const auto* key:{"live_presenter_id","fixed_id","manager_id","loading_id","param_id","manager_param_id"})target[key]=out.state.at(key);
            for(const auto* key:{"button_id","pressed_callback_id"})target[key]=out.state["button"].at(key);
            target["closure_id"]=out.waits[0].observed.at("closure_id");target["source_id"]=out.waits[0].observed.at("source_id");
            out.state["target"]=target;
        }
    }catch(const std::exception& error){out.state["read_errors"].push_back(error.what());out.state["ready"]=false;out.state["target"]=nullptr;}
    return out;
}
}

json read_live_loading(Runtime& r,void* live,void* fixed){
    auto state=observe(r,live,fixed).state;state.erase("last_press");return state;
}
json read_live_loading_receipt(Runtime& r){return last_press(r);}

bool submit_live_loading(Runtime& r,void* live,void* fixed,const json& target,const json& before){
    if(target.value("action_id",std::string())!="live.loading_continue")return false;
    require(before.at("screen_type")=="LiveScenePresenter"&&before.at("busy")==false&&before.at("actions_complete")==true,
        "Live input snapshot is not ready");
    require(before.at("screen_instance_id")==std::to_string(reinterpret_cast<std::uintptr_t>(live)),"snapshot Live owner changed");
    int matches=0;for(const auto& action:before.at("legal_actions"))if(action.at("target")==target)++matches;
    require(matches==1,"target is not the unique native legal candidate");
    auto fresh=observe(r,live,fixed);require(fresh.state.at("ready")==true&&fresh.state.at("target")==target,"loading owner/canvas/waiter changed");
    // Qualify the exact declaring method with the existing read-only metadata
    // inspector. Runtime.method's optional token is an OLD profile token, not
    // an unchecked current token: this newly observed method has no old row.
    const json owner={{"image","quaunity-ui.Runtime.dll"},{"namespace","Qua.UI"},
        {"type_path",json::array({"ButtonBase"})},{"name","OnPressedHandler"},{"arity",1},
        {"token",std::uint32_t{0x060002F8}}};
    const auto inspected=inspect_current_pc_contracts({{"schema","gkms.pc-readonly-contract-request.v1"},
        {"methods",json::array({owner})},{"classes",json::array()}});
    require(inspected.at("complete")==true&&inspected.at("version_stable")==true&&inspected.at("errors").empty()&&
        inspected.at("methods").size()==1,"normal press metadata inspection failed");
    const auto& actual=inspected.at("methods")[0];
    require(actual.at("token")==0x060002F8&&actual.at("static")==false&&
        actual.at("return_type").at("name")=="System.Void","normal press declaring method changed");
    auto method=r.method(r.klass("quaunity-ui.Runtime.dll","Qua.UI","ButtonBase"),"OnPressedHandler",1);
    const auto contract=r.method_parameter_contract(method);const auto& parameter=contract.at("parameters").at(0);
    require(contract.at("parameter_count")==1&&parameter.at("type_name")=="System.Boolean"&&
        parameter.at("type_kind")==2&&parameter.at("byref")==false,"normal press handler signature changed");
    require(status(r,fresh.waits[0].source)==0,"original press waiter is no longer pending");
    std::array<GCHandle,6> held{};const std::array objects={fresh.waits[0].source,fresh.waits[0].closure,fresh.button,fresh.loading,live,fixed};
    try{for(std::size_t i=0;i<held.size();++i)held[i]=r.strong_handle(objects[i]);}
    catch(...){for(auto handle:held)r.free_handle(handle);throw;}
    // Own this observation before invoking a callback that can synchronously
    // resume and dispose the loading view. A completed source stays strongly
    // rooted; no pooled task token or GetResult is involved.
    for(auto handle:last.roots)r.free_handle(handle);last.roots=held;last.terminal=false;
    last.receipt={{"target",target},{"handler_submitted",true},{"handler_returned",false},
        {"status",0},{"status_name","Pending"},{"error",nullptr},
        {"handler","Qua.UI.ButtonBase.OnPressedHandler(System.Boolean)"},{"handler_token",0x060002F8}};
    bool pressed=true;
    try{r.invoke(method,fresh.button,{&pressed});last.receipt["handler_returned"]=true;}
    catch(const std::exception& error){last.receipt["error"]=error.what();last_press(r);throw;}
    last_press(r); // actual source status, never a synthetic success flag
    return true;
}
}
