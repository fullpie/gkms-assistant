#pragma once
#include <cstdint>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace gkms::bridge {
struct ExamSelectorOwner {
    void* command{};
    void* completion{};
    void* wait_runner{};
    void* select_runner{};
    void* handler_runner{};
};

namespace selector_owner_detail {
inline void require(bool value,const char* message){
    if(!value)throw std::runtime_error(std::string("exam selector owner: ")+message);
}
template<class R> bool type(R& r,void* object,const char* name,const char* ns=nullptr){
    return object&&r.class_name(r.object_class(object))==name&&
        (!ns||r.class_namespace(r.object_class(object))==ns);
}
template<class R> void* runner(R& r,void* action){
    if(!type(r,action,"Action","System"))return nullptr;
    auto target=r.getter(action,"get_Target");
    return type(r,target,"AsyncUniTask`1","Cysharp.Threading.Tasks.CompilerServices")||
        type(r,target,"AsyncUniTask`2","Cysharp.Threading.Tasks.CompilerServices")?target:nullptr;
}
template<class R> void* state(R& r,void* runner,const char* prefix,void* owner,int phase){
    require(runner!=nullptr,"missing async runner");
    auto value=r.read_boxed_field(runner,"stateMachine");
    require(value&&r.class_name(r.object_class(value)).rfind(prefix,0)==0,"wrong async state machine");
    require(r.read_object_field(value,"<>4__this")==owner,"async presenter identity differs");
    require(r.template field<int>(value,"<>1__state")==phase,"async handler is not awaiting this selector");
    return value;
}
template<class R> void await_link(R& r,void* state,const char* field,void* source,std::int16_t version){
    auto awaiter=r.read_boxed_field(state,field);
    require(type(r,awaiter,"Awaiter"),"missing typed awaiter");
    auto task=r.read_boxed_field(awaiter,"task");
    require(type(r,task,"UniTask")||type(r,task,"UniTask`1"),"missing typed task");
    require(r.read_object_field(task,"source")==source,"awaiter points to another source");
    require(r.template field<std::int16_t>(task,"token")==version,"awaiter token differs from source version");
}
template<class R> void* pending_core(R& r,void* runner){
    auto core=r.read_boxed_field(runner,"core");
    require(type(r,core,"UniTaskCompletionSourceCore`1","Cysharp.Threading.Tasks"),"missing typed completion core");
    require(r.template field<int>(core,"completedCount")==0&&!r.read_object_field(core,"error"),"async source is already complete");
    return core;
}
template<class R> void* next_runner(R& r,void* core){
    require(r.read_object_field(core,"continuation")!=nullptr,"missing completion continuation");
    auto next=runner(r,r.read_object_field(core,"continuationState"));
    require(next!=nullptr,"completion continuation has no native runner");
    return next;
}
}

// Read the existing OnDestroyAsync waiter. Do not call OnDestroyAsync,
// register callbacks, invoke continuations, GetResult, or add a component.
// The queue head is intentionally not an argument: ExecuteCommandImplAsync
// has already popped the PlayEffect while this await chain still owns it.
template<class R> ExamSelectorOwner read_exam_selector_owner(
    R& r,void* trigger,void* selector,void* screen,void* parameter,int minimum,int maximum){
    using namespace selector_owner_detail;
    require(type(r,trigger,"AsyncDestroyTrigger","Cysharp.Threading.Tasks.Triggers"),"existing destroy trigger unavailable");
    require(!r.template field<bool>(trigger,"called"),"selector destroy trigger already fired");
    auto cts=r.read_object_field(trigger,"cancellationTokenSource");
    require(type(r,cts,"CancellationTokenSource","System.Threading"),"selector cancellation source unavailable");
    require(!r.template field<bool>(cts,"_disposed")&&!r.template unbox<bool>(r.getter(cts,"get_IsCancellationRequested")),"selector cancellation source is closed");
    auto lists=r.read_object_field(cts,"_registeredCallbacksLists");
    require(lists!=nullptr,"selector has no destroy waiters");
    std::set<void*> fragments,callbacks;
    std::vector<ExamSelectorOwner> matches;
    for(auto list:r.enumerate(lists,64)){
        if(!list)continue;
        require(type(r,list,"SparselyPopulatedArray`1","System.Threading"),"unexpected callback list type");
        for(auto fragment=r.read_object_field(list,"_head");fragment;fragment=r.read_object_field(fragment,"_next")){
            require(fragments.size()<64&&fragments.insert(fragment).second,"cyclic or excessive callback fragments");
            require(type(r,fragment,"SparselyPopulatedArrayFragment`1","System.Threading"),"unexpected callback fragment type");
            for(auto info:r.enumerate(r.read_object_field(fragment,"_elements"),1024)){
                if(!info)continue;
                require(callbacks.size()<1024&&callbacks.insert(info).second,"duplicate or excessive destroy callback");
                require(type(r,info,"CancellationCallbackInfo","System.Threading")&&r.read_object_field(info,"CancellationTokenSource")==cts,"callback cancellation source differs");
                auto completion=r.read_object_field(info,"StateForCallback");
                if(!type(r,completion,"UniTaskCompletionSource","Cysharp.Threading.Tasks"))continue;
                auto wait=runner(r,r.read_object_field(completion,"singleState"));
                if(!wait)continue;
                auto wait_state=r.read_boxed_field(wait,"stateMachine");
                if(!wait_state||r.class_name(r.object_class(wait_state)).rfind("<WaitSelectAsync>d__",0)!=0||
                    r.read_object_field(wait_state,"<>4__this")!=selector)continue;
                require(r.template field<int>(completion,"intStatus")==0&&!r.template field<bool>(completion,"handled")&&
                    r.read_object_field(completion,"singleContinuation"),"destroy waiter is no longer pending");
                auto secondary=r.read_object_field(completion,"secondaryContinuationList");
                require(!secondary||r.enumerate(secondary,16).empty(),"destroy waiter has multiple continuations");
                wait_state=state(r,wait,"<WaitSelectAsync>d__",selector,0);
                await_link(r,wait_state,"<>u__1",completion,0);
                auto wait_core=pending_core(r,wait);
                auto select=next_runner(r,wait_core);
                auto select_state=state(r,select,"<SelectCardAsync>d__",screen,1);
                await_link(r,select_state,"<>u__2",wait,r.template field<std::int16_t>(wait_core,"version"));
                auto select_core=pending_core(r,select);
                auto handler=next_runner(r,select_core);
                auto handler_state=state(r,handler,"<Campus-InGame-Exam-IExamSequenceHandler-OnEffectSelectParameterAsync>d__",screen,0);
                await_link(r,handler_state,"<>u__1",select,r.template field<std::int16_t>(select_core,"version"));
                pending_core(r,handler);
                auto context=r.read_object_field(handler_state,"context");
                require(context&&r.read_object_field(context,"<ExamParameter>k__BackingField")==parameter&&
                    !r.template field<bool>(context,"_disposed"),"effect handler belongs to another exam context");
                require(r.template field<int>(handler_state,"pickMin")==minimum&&r.template field<int>(handler_state,"pickMax")==maximum&&
                    r.template field<int>(select_state,"countMin")==minimum&&r.template field<int>(select_state,"countMax")==maximum,"selector count contract differs from handler");
                auto command=r.read_object_field(handler_state,"command");
                require(type(r,command,"ExamPlayCommand"),"effect handler has no native command");
                matches.push_back({command,completion,wait,select,handler});
            }
        }
    }
    require(matches.size()==1,"selector does not have one unique active effect owner");
    return matches.front();
}
} // namespace gkms::bridge
