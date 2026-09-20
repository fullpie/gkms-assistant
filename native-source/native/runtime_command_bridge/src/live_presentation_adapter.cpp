#include "live_presentation_adapter.hpp"
#include "outer_adapter.hpp"
#include "story_adapter.hpp"
#include "outer_pointer_guard.hpp"
#include "live_loading_adapter.hpp"
#include <cmath>

namespace gkms::bridge {
namespace {
bool flag(Runtime& r,void* object,const char* name){return r.unbox<bool>(r.getter(object,name));}
std::string text(Runtime& r,void* object,const char* name){return r.string(r.getter(object,name));}
bool active(Runtime& r,void* object){return object&&flag(r,r.getter(object,"get_gameObject"),"get_activeInHierarchy");}
void* getter(Runtime& r,void* object,const char* name,std::uint32_t token){return r.invoke(r.method(r.object_class(object),name,0,token),object);}
bool has_existing_active_layer(Runtime& r){
    auto type=r.klass("Assembly-CSharp","Campus.Common","ScreenLayerManager");
    // PureSingleton.HasInstance only observes the existing static instance.
    // Do not call its allocating get_Instance path when it does not exist.
    if(!r.unbox<bool>(r.invoke(r.method(type,"get_HasInstance",0,0x06000E83),nullptr)))return false;
    auto manager=r.invoke(r.method(type,"get_Instance",0,0x06000E82),nullptr);
    return manager&&flag(r,manager,"get_HasActive");
}
void* current_live_presenter(Runtime& r){
    auto type=r.klass("Assembly-CSharp","Campus.Live","LiveScenePresenter");
    auto unity=r.klass("UnityEngine.CoreModule","UnityEngine","Object");
    auto objects=r.invoke(r.method(unity,"FindObjectsOfType",1,0x06001546),nullptr,{r.reflection_type(type)});
    void* presenter{};int count=0;
    for(auto candidate:r.enumerate(objects,16)){
        if(!candidate||r.class_name(r.object_class(candidate))!="LiveScenePresenter"||!active(r,candidate))continue;
        presenter=candidate;++count;
    }
    return count==1&&!has_existing_active_layer(r)?presenter:nullptr;
}
}
json read_live_presentation_snapshot(Runtime& r,const std::string& generation){
    auto presenter=current_live_presenter(r);
    if(!presenter)return nullptr;
    auto model=r.read_object_field(presenter,"_liveModel");
    if(!model)return nullptr; // no fabricated loading identity before InitializeModel
    auto fixed=getter(r,model,"get_FixedData",0x06000EC4);
    auto execution=getter(r,model,"get_ExecutionData",0x06000EC6);
    if(!fixed||!execution)return nullptr;
    const json identity={{"live_from_type",r.unbox<int>(getter(r,fixed,"get_LiveFromType",0x06001261))},
        {"idol_card_id",r.string(getter(r,fixed,"get_IdolCardId",0x0600125A))},
        {"character_id",r.string(getter(r,fixed,"get_CharacterId",0x06001259))}};
    auto user=r.klass("Assembly-CSharp","Campus.Common.User","UserDataManager");
    auto progress_object=r.invoke(r.method(user,"get_UserProduceProgress",0),nullptr);
    if(!progress_object)return nullptr;
    const json actual_identity={{"in_progress",flag(r,progress_object,"get_IsInProgress")},
        {"produce_id",text(r,progress_object,"get_ProduceId")},
        {"idol_card_id",text(r,progress_object,"get_IdolCardId")},
        {"character_id",text(r,progress_object,"get_CharacterId")}};
    if(!live_presentation_identity_matches(actual_identity,identity,1,false))return nullptr;
    auto progress=json::parse(text(r,progress_object,"ToString"));
    auto state=native_outer_progress_state(r,progress_object);state.update(actual_identity);
    auto live=identity;
    live["is_live_started"]=r.unbox<bool>(getter(r,execution,"get_IsLiveStarted",0x06001245));
    live["is_live_ended"]=r.unbox<bool>(getter(r,execution,"get_IsLiveEnded",0x06001247));
    live["is_playing"]=r.unbox<bool>(getter(r,execution,"get_IsPlaying",0x06001249));
    live["is_pause"]=r.unbox<bool>(getter(r,execution,"get_IsPause",0x0600124A));
    auto time_property=getter(r,execution,"get_CurrentTime",0x0600124C);
    live["current_time"]=nullptr;
    if(time_property){
        const float seconds=r.unbox<float>(r.getter(time_property,"get_Value"));
        if(!std::isfinite(seconds))throw std::runtime_error("native live presentation time is not finite");
        live["current_time"]=seconds;
    }
    auto scene_model=r.read_object_field(presenter,"_sceneModel");
    live["scene_is_end_live"]=scene_model?json(r.unbox<bool>(getter(r,scene_model,"get_IsEndLive",0x0600114C))):json(nullptr);
    live["is_playing_tutorial"]=scene_model?json(r.unbox<bool>(getter(r,scene_model,"get_IsPlayingTutorial",0x06001150))):json(nullptr);
    auto latest_progress=r.invoke(r.method(user,"get_UserProduceProgress",0),nullptr);
    if(!active(r,presenter)||has_existing_active_layer(r)||r.read_object_field(presenter,"_liveModel")!=model||
        !latest_progress||json::parse(text(r,latest_progress,"ToString"))!=progress)
        throw std::runtime_error("live presentation owner or produce progress changed while copying");
    auto snapshot=make_live_presentation_snapshot(generation,std::to_string(reinterpret_cast<std::uintptr_t>(presenter)),
        progress,state,live,utc_now());
    append_story_actions(r,presenter,"LiveScenePresenter",snapshot);
    snapshot["ui_state"]["loading"]=read_live_loading(r,presenter,fixed);
    snapshot["live_loading_receipt"]=read_live_loading_receipt(r);
    const auto& loading=snapshot["ui_state"]["loading"];
    if(snapshot["ui_state"]["phase"]=="before_live"&&snapshot["busy"]==false&&
       snapshot["legal_actions"].empty()&&loading.at("ready")==true){
        snapshot["legal_actions"].push_back({{"action_id","live.loading_continue"},{"target",loading.at("target")},
            {"evidence",{{"source","owned LiveLoading normal press with original Pending WaitPress"}}}});
        snapshot["actions_complete"]=true;
    }
    snapshot["ui_state"]["input_projection"]="owned SimpleHorizontal story skip-only and current LiveLoading normal press; Live menu/photo/AllSkip not projected";
    snapshot["pointer_blocking"]=read_outer_pointer_guard(r,presenter,false);
    if(snapshot.at("busy")==true||snapshot.at("pointer_blocking").at("input_ready")!=true){
        snapshot["legal_actions"]=json::array();snapshot["actions_complete"]=false;
    }
    if(current_live_presenter(r)!=presenter||r.read_object_field(presenter,"_liveModel")!=model)
        throw std::runtime_error("Live story owner changed while copying controls");
    auto revision_view=snapshot;revision_view.erase("revision");revision_view.erase("captured_at");
    revision_view["ui_state"]["loading"]=live_loading_revision_view(revision_view["ui_state"]["loading"]);
    snapshot["revision"]=sha256(generation+revision_view.dump());return snapshot;
}
bool submit_live_presentation_action(Runtime& r,const json& target,const json& before){
    if(before.value("screen_type",std::string())!="LiveScenePresenter")return false;
    auto presenter=current_live_presenter(r);
    if(!presenter||before.at("screen_instance_id")!=std::to_string(reinterpret_cast<std::uintptr_t>(presenter))||
        read_outer_pointer_guard(r,presenter,false).at("input_ready")!=true)
        throw std::runtime_error("Live presentation owner or input readiness changed");
    if(target.value("action_id",std::string())=="live.loading_continue"){
        if(before.at("ui_state").at("phase")!="before_live")throw std::runtime_error("Live loading phase changed");
        auto model=r.read_object_field(presenter,"_liveModel");
        auto fixed=model?getter(r,model,"get_FixedData",0x06000EC4):nullptr;
        return submit_live_loading(r,presenter,fixed,target,before);
    }
    if(target.value("action_id",std::string())!="story.skip"||
        target.value("button_source",std::string())!="player-skip-only")
        throw std::runtime_error("Live normal skip-only recipient changed");
    if(!submit_story_action(r,presenter,target,before))throw std::runtime_error("Live story control was not handled");
    return true;
}
}
