#include "screen_context.hpp"
#include "native_trace.hpp"
#include <unordered_set>

namespace gkms::bridge {
namespace {
void* window_class{};
void* title_class{};
void* find_objects{};
bool active_component(Runtime& runtime,void* component) {
    auto game_object=runtime.getter(component,"get_gameObject");
    return game_object&&runtime.unbox<bool>(runtime.getter(game_object,"get_activeInHierarchy"));
}
void* current_root(Runtime& runtime) {
    if(!window_class||!find_objects)throw std::runtime_error("native screen discovery not initialized");
    trace_mark("screen.reflection_type.before",{{"class",reinterpret_cast<std::uintptr_t>(window_class)}});
    auto type=runtime.reflection_type(window_class);
    trace_mark("screen.reflection_type.after",{{"type",reinterpret_cast<std::uintptr_t>(type)}});
    trace_mark("screen.find_objects.before");
    auto array=runtime.invoke(find_objects,nullptr,{type});
    trace_mark("screen.find_objects.after",{{"array",reinterpret_cast<std::uintptr_t>(array)}});
    const auto windows=runtime.enumerate(array,256);
    trace_mark("screen.enumerate.after",{{"count",windows.size()}});
    std::unordered_set<void*> roots;
    for(auto window:windows){
        trace_mark("screen.window.before",{{"object",reinterpret_cast<std::uintptr_t>(window)}});
        if(!window||!active_component(runtime,window))continue;
        // Nested windows belong to their parent's tree. Only an active,
        // parentless window can own the actual top-screen ordering.
        if(runtime.getter(window,"get_Parent")!=nullptr)continue;
        auto root=runtime.getter(window,"get_Root");
        if(root&&root!=window)continue;
        roots.insert(window);
        trace_mark("screen.root.accepted",{{"class",runtime.class_name(runtime.object_class(window))}});
    }
    if(roots.empty())return nullptr;
    if(roots.size()!=1)throw std::runtime_error("native-screen-root-ambiguous");
    trace_mark("screen.root.unique");
    return *roots.begin();
}
void* current_title(Runtime& runtime){
    trace_mark("screen.title_query.before");
    auto array=runtime.invoke(find_objects,nullptr,{runtime.reflection_type(title_class)});
    void* title{};
    for(auto candidate:runtime.enumerate(array,16)){
        if(!candidate||!active_component(runtime,candidate))continue;
        if(title)throw std::runtime_error("native-title-presenter-ambiguous");title=candidate;
    }
    if(!title)throw std::runtime_error("native-screen-root-unavailable");
    trace_mark("screen.title_query.after");return title;
}
}
void initialize_screen_context(Runtime& runtime) {
    // No detour: tiny IL2CPP getters may share native code with unrelated
    // getters. Query Unity's registry by Type and invoke exact MethodInfo.
    window_class=runtime.klass("quaunity-ui.Runtime","Qua.UI","WindowPresenterBase");
    title_class=runtime.klass("Assembly-CSharp","Campus.Title","TitlePresenter");
    auto object=runtime.klass("UnityEngine.CoreModule","UnityEngine","Object");
    find_objects=runtime.method(object,"FindObjectsOfType",1,0x06001546);
}
void* active_screen(Runtime& runtime) {
    auto root=current_root(runtime);
    if(!root)return current_title(runtime);
    trace_mark("screen.top.before");
    auto screen=runtime.getter(root,"get_TopScreen");
    trace_mark("screen.top.after",{{"object",reinterpret_cast<std::uintptr_t>(screen)}});
    if(!screen||!active_component(runtime,screen))throw std::runtime_error("native-top-screen-not-active");
    return screen;
}
void* active_layer(Runtime& runtime) {
    trace_mark("screen.layer.before");
    auto klass=runtime.klass("Assembly-CSharp","Campus.Common","ScreenLayerManager");
    auto manager=runtime.invoke(runtime.method(klass,"get_Instance",0),nullptr);
    if(!manager||!runtime.unbox<bool>(runtime.getter(manager,"get_HasActive"))){trace_mark("screen.layer.none");return nullptr;}
    auto layer=runtime.getter(manager,"GetTopLayer");
    if(!layer||!active_component(runtime,layer))throw std::runtime_error("native-top-layer-not-active");
    trace_mark("screen.layer.after",{{"class",runtime.class_name(runtime.object_class(layer))}});
    return layer;
}
json screen_context_snapshot(Runtime& runtime) {
    auto root=current_root(runtime);
    if(!root){
        auto title=current_title(runtime);auto layer=active_layer(runtime);
        const bool moving=runtime.field<bool>(title,"_isMovingToOtherScene");
        return {{"screen_type","TitlePresenter"},{"tree_busy",moving},{"busy",moving||layer!=nullptr},
            {"has_active_layer",layer!=nullptr},{"layer_type",layer?json(runtime.class_name(runtime.object_class(layer))):json(nullptr)},
            {"active",true}};
    }
    auto screen=runtime.getter(root,"get_TopScreen");
    if(!screen||!active_component(runtime,screen))throw std::runtime_error("native-top-screen-not-active");
    const bool tree_busy=runtime.unbox<bool>(runtime.getter(root,"get_IsBusyTree"));
    auto layer=active_layer(runtime);
    return {{"screen_type",runtime.class_name(runtime.object_class(screen))},
        {"tree_busy",tree_busy},{"busy",tree_busy||layer!=nullptr},
        {"has_active_layer",layer!=nullptr},{"layer_type",layer?json(runtime.class_name(runtime.object_class(layer))):json(nullptr)},
        {"active",true}};
}
}
