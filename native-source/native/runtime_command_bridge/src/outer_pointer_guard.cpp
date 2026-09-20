#include "outer_pointer_guard.hpp"
#include "pointer_identity.hpp"
#include <array>
#include <utility>

namespace gkms::bridge {
namespace {
bool derives(Runtime& r,void* klass,const char* name){
    for(auto current=klass;current;current=r.parent(current))if(r.class_name(current)==name)return true;
    return false;
}
void* get(Runtime& r,void* object,const char* name,std::uint32_t token){
    return r.invoke(r.method(r.object_class(object),name,0,token),object);
}
int sorting_value(Runtime& r,int layer){
    auto utility=r.klass("Assembly-CSharp","Campus.Common","UILayerUtility");
    auto name=r.invoke(r.method(utility,"GetName",1,0x060158F4),nullptr,{&layer});
    auto sorting=r.klass("UnityEngine.CoreModule","UnityEngine","SortingLayer");
    int id=r.unbox<int>(r.invoke(r.method(sorting,"NameToID",1,0x060001FE),nullptr,{name}));
    return r.unbox<int>(r.invoke(r.method(sorting,"GetLayerValueFromID",1,0x060001FC),nullptr,{&id}));
}
bool active(Runtime& r,void* component){
    return component&&r.unbox<bool>(r.getter(r.getter(component,"get_gameObject"),"get_activeInHierarchy"));
}
void* bound_effect_button(Runtime& r,void* presenter,const json& target){
    const auto screen=r.class_name(r.object_class(presenter));
    if(!has_effect_confirmation_lifecycle(screen)||target.at("presenter_type")!=screen||
       target.at("button_source")!="produce-screen-touch"||
       target.at("screen_instance_id")!=std::to_string(reinterpret_cast<std::uintptr_t>(presenter)))
        throw std::runtime_error("effect pointer recipient owner changed");
    auto type=r.klass("Assembly-CSharp","Campus.InGame.Produce","ProduceScenePresenter");
    auto scene=r.invoke(r.method(type,"get_Instance",0),nullptr);
    if(!scene||r.class_name(r.object_class(scene))!="ProduceScenePresenter"||!active(r,scene))
        throw std::runtime_error("effect pointer ProduceScene is unavailable");
    auto view=r.read_object_field(scene,"_view");
    if(!view)throw std::runtime_error("effect pointer ProduceScene view is unavailable");
    auto button=get(r,view,"get_OverlayButton",0x06003F6D);
    auto callback=button?r.read_object_field(button,"onClickedCallback"):nullptr;
    if(!active(r,button)||!r.unbox<bool>(r.getter(button,"get_IsEnabled"))||
       r.unbox<bool>(r.getter(button,"get_IsDisabled"))||!callback||
       target.at("button_instance_id")!=pointer_identity(button)||
       target.at("wait_callback_instance_id")!=pointer_identity(callback))
        throw std::runtime_error("effect pointer current button or wait callback changed");
    auto manager=r.klass("Assembly-CSharp","Campus.Common.User","UserDataManager");
    auto progress=r.invoke(r.method(manager,"get_UserProduceProgress",0),nullptr);
    if(!progress||target.at("produce_id")!=r.string(r.getter(progress,"get_ProduceId"))||
       target.at("week")!=r.unbox<int>(r.getter(progress,"get_StepNumber"))||
       target.at("step_type")!=r.unbox<int>(r.getter(progress,"get_StepType")))
        throw std::runtime_error("effect pointer current Produce/week/step changed");
    return button;
}
json effect_canvas_ancestors(Runtime& r,void* button){
    auto type=r.reflection_type(r.klass("UnityEngine.UIModule","UnityEngine","Canvas"));
    auto values=r.invoke(r.method(r.object_class(button),"GetComponentsInParent",1,0x060013CF),button,{type});
    const auto canvases=r.enumerate(values,64);
    std::vector<std::pair<void*,void*>> canvas_transforms;
    for(auto canvas:canvases)canvas_transforms.emplace_back(canvas,r.getter(canvas,"get_transform"));
    auto transform=r.getter(button,"get_transform");json result=json::array();
    auto sorting=r.klass("UnityEngine.CoreModule","UnityEngine","SortingLayer");
    for(int distance=0;transform&&distance<128;++distance){
        void* selected{};
        for(const auto& [canvas,parent]:canvas_transforms)if(parent==transform){
            if(selected)throw std::runtime_error("effect pointer has ambiguous Canvas components at one transform");selected=canvas;
        }
        if(selected){
            auto root=get(r,selected,"get_rootCanvas",0x06000094);
            int layer_id=r.unbox<int>(get(r,selected,"get_sortingLayerID",0x0600008D));
            const bool is_root=r.unbox<bool>(get(r,selected,"get_isRootCanvas",0x06000078));
            result.push_back({{"instance_id",pointer_identity(selected)},{"hierarchy_distance",distance},
                {"root_canvas_instance_id",pointer_identity(root)},
                {"active_and_enabled",r.unbox<bool>(get(r,selected,"get_isActiveAndEnabled",0x060013AB))},
                {"override_sorting",r.unbox<bool>(get(r,selected,"get_overrideSorting",0x06000087))},
                {"is_root_canvas",is_root},{"sorting_layer_id",layer_id},
                {"sorting_layer_value",r.unbox<int>(r.invoke(r.method(sorting,"GetLayerValueFromID",1,0x060001FC),nullptr,{&layer_id}))},
                {"sorting_order",r.unbox<int>(get(r,selected,"get_sortingOrder",0x06000089))},
                {"render_mode",r.unbox<int>(get(r,selected,"get_renderMode",0x06000076))}});
            if(is_root)return result;
        }
        transform=r.getter(transform,"get_parent");
    }
    if(transform)throw std::runtime_error("effect pointer Canvas ancestry exceeds bound");
    return result;
}
}
json read_outer_pointer_guard(Runtime& r,void* presenter,bool is_screen_layer,const json& input_target){
    std::array<int,2> orders={13,17},counts={0,0};
    auto manager_class=r.klass("Assembly-CSharp","Campus.Common","CampusBlockingManager");
    if(r.unbox<bool>(r.invoke(r.method(manager_class,"get_ExistInstance",0,0x06000093),nullptr))){
        auto manager=r.invoke(r.method(manager_class,"get_Instance",0,0x06000092),nullptr);
        auto counters=r.read_object_field(manager,"_blockingCounter");
        auto lookup=r.method(r.object_class(counters),"TryGetValue",2);
        for(std::size_t index=0;index<orders.size();++index)r.invoke(lookup,counters,{&orders[index],&counts[index]});
    }
    json guard={{"content_blocking_count",counts[0]},{"dialog_blocking_count",counts[1]},
        {"input_ready",true},{"input_source","no-active-native-pointer-blocker"},
        {"input_layer",nullptr},{"input_sorting_layer_value",nullptr},{"input_order_in_layer",nullptr},
        {"blocking_layers",json::array()}};
    bool known=false;int input_value{};std::optional<int> input_order;
    if(input_target.is_object()&&input_target.value("action_id",std::string())=="effect.advance"){
        guard["input_source"]="ProduceScene.OverlayButton.actual-effective-Canvas";
        guard["input_target"]=input_target;
        try{
            auto button=bound_effect_button(r,presenter,input_target);
            const auto ancestors=effect_canvas_ancestors(r,button);
            guard["recipient_canvas_ancestors"]=ancestors;
            const auto canvas=effective_pointer_canvas(ancestors);
            if(canvas.is_null())throw std::runtime_error("effect pointer effective Canvas is unresolved");
            guard["effective_input_canvas"]=canvas;
            input_value=canvas.at("sorting_layer_value").get<int>();input_order=canvas.at("sorting_order").get<int>();known=true;
        }catch(const std::exception& error){
            guard["input_ready"]=false;guard["input_source"]="effect-recipient-with-unresolved-current-Canvas";
            guard["recipient_error"]=error.what();
        }
    }else if(is_screen_layer){
        const int layer=r.unbox<int>(r.getter(presenter,"GetCanvasLayer"));
        input_value=sorting_value(r,layer);known=true;
        guard["input_layer"]=layer;guard["input_source"]="foreground-screen-layer.GetCanvasLayer";
    }else if(presenter&&r.has_field(r.object_class(presenter),"_view")){
        auto view=r.read_object_field(presenter,"_view");
        if(view&&derives(r,r.object_class(view),"CampusScreenViewBase")){
            auto common=get(r,view,"get_CommonView",0x06000623);
            auto canvas=get(r,common,"get_Canvas",0x060005F4);
            input_value=r.unbox<int>(get(r,canvas,"get_LayerValue",0x06001148));
            input_order=r.unbox<int>(get(r,canvas,"get_OrderInLayer",0x06001149));known=true;
            guard["input_source"]="CampusScreenCommonView.Canvas.actual-sorting-layer-and-order";
        }
    }
    if(known)guard["input_sorting_layer_value"]=input_value;
    if(input_order)guard["input_order_in_layer"]=*input_order;
    if(!known&&!guard.contains("input_target")&&(counts[0]>0||counts[1]>0))guard["input_source"]="active-blocker-with-unresolved-input-canvas";
    for(std::size_t index=0;index<orders.size();++index){
        if(counts[index]<=0)continue;
        const int value=sorting_value(r,orders[index]);
        // APK CampusBlockingManager.ShowBlocking passes depth=blockingOrder
        // to BlockingManagerBase.Show; CampusBlockingView.SetDepth assigns
        // that depth to Canvas.sortingOrder. Compare actual sorting values,
        // not a presumed numerical ordering of Unity sorting-layer IDs.
        const bool blocks=outer_pointer_blocked(counts[index],known,input_value,input_order,value,orders[index]);
        guard["blocking_layers"].push_back({{"order",orders[index]},{"count",counts[index]},
            {"sorting_layer_value",value},{"order_in_layer",orders[index]},{"blocks_input",blocks}});
        if(blocks)guard["input_ready"]=false;
    }
    return guard;
}
}
