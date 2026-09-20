#pragma once
#include "runtime.hpp"
#include "pointer_identity.hpp"

namespace gkms::bridge {
inline void* native_layer_manager(Runtime& r){
    auto type=r.klass("Assembly-CSharp","Campus.Common","ScreenLayerManager");
    return r.invoke(r.method(type,"get_Instance",0),nullptr);
}
inline void* native_layer_parent(Runtime& r,void* layer){
    return r.invoke(r.method(r.object_class(layer),"Campus.Common.IScreenLayer.GetParent",0,0x06015B4E),layer);
}
inline void* native_screen_sheet_root(Runtime& r,void* screen){
    auto manager=native_layer_manager(r);
    return manager&&screen?r.invoke(r.method(r.object_class(manager),"GetRoot",1,0x06015AFE),manager,{screen}):nullptr;
}
inline int registered_layer_predecessor_index(const json& rows,const std::string& current_top){
    int previous=-1,last=-1;
    for(std::size_t i=0;i<rows.size();++i){
        if(rows[i].at("active")!=true)continue;
        previous=last;last=static_cast<int>(i);
    }
    if(last<0||rows[last].at("instance_id")!=current_top)return -1;
    return previous;
}
inline void* registered_layer_predecessor(Runtime& r,void* child,json* diagnostic=nullptr){
    // ScreenLayerManager.GetTopLayer is LastOrDefault(IsActiveInHierarchy)
    // over _layers. Use that same native registry/order, not scene discovery.
    auto manager=native_layer_manager(r);
    json rows=json::array();std::vector<void*> layers;
    void* top{};
    if(manager){
        top=r.getter(manager,"GetTopLayer");
        auto registered=r.read_object_field(manager,"_layers");
        if(registered)layers=r.enumerate(registered,128);
        for(auto layer:layers){
            const bool active=layer&&r.unbox<bool>(r.invoke(r.method(r.object_class(layer),
                "Campus.Common.IScreenLayer.IsActiveInHierarchy",0,0x06015B51),layer));
            rows.push_back({{"instance_id",pointer_identity(layer)},
                {"screen_type",layer?r.class_name(r.object_class(layer)):std::string()},
                {"active",active}});
        }
    }
    const int index=top==child?registered_layer_predecessor_index(rows,pointer_identity(child)):-1;
    if(diagnostic)*diagnostic={{"source","ScreenLayerManager._layers/LastOrDefault(IsActiveInHierarchy)"},
        {"current_top_instance_id",pointer_identity(top)},{"layers",rows},
        {"predecessor_index",index<0?json(nullptr):json(index)}};
    return index<0?nullptr:layers[static_cast<std::size_t>(index)];
}
}
