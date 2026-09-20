#pragma once
#include "exam_selector_owner.hpp"
#include "exam_card_selector_family.hpp"

namespace gkms::bridge {
struct ExamCloseSelectorOwner {
    void* command{};
    void* callback{};
    void* completion{};
    void* event_runner{};
    void* open_runner{};
    void* create_runner{};
    void* select_runner{};
    void* handler_runner{};
    void* execute_runner{};
};

namespace close_owner_detail {
using namespace selector_owner_detail;
template<class R> void* state(R& r,void* runner,const char* prefix,int phase){
    require(runner!=nullptr,"missing close-await runner");
    auto value=r.read_boxed_field(runner,"stateMachine");
    require(value&&r.class_name(r.object_class(value)).rfind(prefix,0)==0,"wrong close-await state machine");
    require(r.template field<int>(value,"<>1__state")==phase,"close handler is not in its observed await phase");
    return value;
}
template<class R> void same_count(R& r,void* state,const char* minimum,const char* maximum,int lo,int hi){
    require(r.template field<int>(state,minimum)==lo&&r.template field<int>(state,maximum)==hi,
        "close selector counts differ from its creator");
}
}

// Current PC: ScreenLayerManager._OpenAsync waits for onClose before returning
// the presenter to CreateCardSelectorImplAsync/SelectCardAsync. WaitSelectAsync
// and its AsyncDestroyTrigger have not started while the visible picker waits.
// Follow the existing event registration; never register/fire/consume it.
template<class R> ExamCloseSelectorOwner read_exam_close_selector_owner(
    R& r,void* selector,void* screen,void* sequence,void* parameter,int minimum,int maximum){
    using namespace selector_owner_detail;
    const int create_phase=selector?exam_card_selector_create_phase(r.class_name(r.object_class(selector))):-1;
    require(create_phase>=0&&r.class_namespace(r.object_class(selector))=="Campus.InGame.Exam"&&
        type(r,screen,"ExamScreenPresenter","Campus.InGame.Exam"),"close selector/screen type unavailable");
    auto on_close=r.read_object_field(selector,"onClose");
    require(type(r,on_close,"Action","System"),"selector close event unavailable");
    std::vector<ExamCloseSelectorOwner> matches;
    std::set<void*> seen;
    for(auto callback:r.enumerate(r.getter(on_close,"GetInvocationList"),64)){
        require(callback&&seen.insert(callback).second,"duplicate close callback registration");
        if(!type(r,callback,"Action","System"))continue;
        auto closure=r.getter(callback,"get_Target");
        if(!closure||!r.has_field(r.object_class(closure),"utcs")||
            !r.has_field(r.object_class(closure),"onExecute")||!r.has_field(r.object_class(closure),"removeHandler"))continue;
        auto completion=r.read_object_field(closure,"utcs");
        if(!type(r,completion,"UniTaskCompletionSource","Cysharp.Threading.Tasks"))continue;
        require(r.read_object_field(closure,"onExecute")==callback,"close event differs from its completion callback");
        require(r.template field<int>(completion,"intStatus")==0&&!r.template field<bool>(completion,"handled")&&
            r.read_object_field(completion,"singleContinuation"),"close event completion is no longer pending");
        auto secondary=r.read_object_field(completion,"secondaryContinuationList");
        require(!secondary||r.enumerate(secondary,16).empty(),"close event has multiple continuations");
        auto event_runner=runner(r,r.read_object_field(completion,"singleState"));
        auto event=close_owner_detail::state(r,event_runner,"<FromEventAsync>d__0",0);
        require(r.read_object_field(event,"<>8__1")==closure,"event awaiter belongs to another registration");
        await_link(r,event,"<>u__1",completion,0);
        auto event_core=pending_core(r,event_runner);
        auto open_runner=next_runner(r,event_core);
        auto open=close_owner_detail::state(r,open_runner,"<_OpenAsync>d__6`1",3);
        require(type(r,r.read_object_field(open,"<>4__this"),"ScreenLayerManager","Campus.Common"),"close waiter has no native screen manager");
        auto open_closure=r.read_object_field(open,"<>8__1");
        require(open_closure&&r.read_object_field(open_closure,"presenter")==selector,"open waiter belongs to another selector");
        auto remove=r.read_object_field(closure,"removeHandler");
        require(type(r,remove,"Action`1","System")&&r.getter(remove,"get_Target")==open_closure&&
            r.read_object_field(event,"removeHandler")==remove,"close event removal belongs to another open operation");
        await_link(r,open,"<>u__1",event_runner,r.template field<std::int16_t>(event_core,"version"));
        auto open_core=pending_core(r,open_runner);
        auto create_runner=next_runner(r,open_core);
        auto create=close_owner_detail::state(r,create_runner,"<CreateCardSelectorImplAsync>d__4",create_phase);
        await_link(r,create,exam_card_selector_create_awaiter(create_phase),open_runner,r.template field<std::int16_t>(open_core,"version"));
        close_owner_detail::same_count(r,create,"countMin","countMax",minimum,maximum);
        auto create_core=pending_core(r,create_runner);
        auto select_runner=next_runner(r,create_core);
        auto select=close_owner_detail::state(r,select_runner,"<SelectCardAsync>d__143",0);
        require(r.read_object_field(select,"<>4__this")==screen,"card selection belongs to another Exam screen");
        await_link(r,select,"<>u__1",create_runner,r.template field<std::int16_t>(create_core,"version"));
        close_owner_detail::same_count(r,select,"countMin","countMax",minimum,maximum);
        auto select_core=pending_core(r,select_runner);
        auto handler_runner=next_runner(r,select_core);
        auto handler=close_owner_detail::state(r,handler_runner,"<Campus-InGame-Exam-IExamSequenceHandler-OnEffectSelectParameterAsync>d__142",0);
        require(r.read_object_field(handler,"<>4__this")==screen,"effect selection belongs to another Exam screen");
        await_link(r,handler,"<>u__1",select_runner,r.template field<std::int16_t>(select_core,"version"));
        close_owner_detail::same_count(r,handler,"pickMin","pickMax",minimum,maximum);
        auto context=r.read_object_field(handler,"context");
        require(context&&r.read_object_field(context,"<ExamParameter>k__BackingField")==parameter&&
            r.read_object_field(context,"<CommandExecutor>k__BackingField")==sequence&&
            !r.template field<bool>(context,"_disposed"),"effect context belongs to another active sequence");
        auto command=r.read_object_field(handler,"command");
        require(type(r,command,"ExamPlayCommand","Campus.InGame.Exam"),"effect handler has no actual native command");
        auto handler_core=pending_core(r,handler_runner);
        auto execute_runner=next_runner(r,handler_core);
        auto execute=close_owner_detail::state(r,execute_runner,"<ExecuteCommandImplAsync>d__111",5);
        require(r.read_object_field(execute,"<>4__this")==sequence&&r.read_object_field(execute,"command")==command,
            "native executing command differs from selector handler");
        pending_core(r,execute_runner);
        matches.push_back({command,callback,completion,event_runner,open_runner,create_runner,select_runner,handler_runner,execute_runner});
    }
    require(matches.size()==1,"selector does not have one unique active close owner");
    return matches.front();
}
} // namespace gkms::bridge
