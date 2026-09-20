#include "exam_adapter.hpp"
#include "screen_context.hpp"
#include "produce_lifecycle_adapter.hpp"
#include "pointer_identity.hpp"
#include "audition_identity.hpp"
#include "exam_legal_inputs.hpp"
#include "exam_model_observation.hpp"
#include "pc_method_binding.hpp"
#include "exam_model_revision.hpp"
#include "exam_reference_presence.hpp"
#include <array>
#include <filesystem>
#include <fstream>

namespace gkms::bridge {
namespace {
std::string pointer_id(void* value) { return pointer_identity(value); }
void verify_current_pc() {
    verify_current_pc_method_profile();
}
}
void ExamAdapter::initialize(Runtime& runtime) {
    verify_current_pc();
    runtime_=&runtime;
    auto presenter=runtime.klass("Assembly-CSharp","Campus.InGame.Exam","ExamScreenPresenter");
    auto sequence=runtime.klass("Assembly-CSharp","Campus.InGame.Exam","ExamSequence");
    auto command=runtime.klass("Assembly-CSharp","Campus.InGame.Exam","ExamPlayCommand");
    auto utility=runtime.klass("Assembly-CSharp","Campus.InGame.Exam","ExamCardUtility");
    save_serializer_.initialize(runtime);
    presenter_method_=runtime.method(presenter,"get_Sequence",0,0x06005567);
    hand_factory_=runtime.method(command,"CreateUseHandCommand",3,0x06004DCE);
    drink_factory_=runtime.method(command,"CreateUseDrinkCommand",3,0x06004DD0);
    end_factory_=runtime.method(command,"CreateTurnEndCommand",1,0x06004DD9);
    enqueue_=runtime.method(sequence,"AddExecuteCommand",1,0x06004E58);
    create_context_=runtime.method(sequence,"CreateEffectResolver",0,0x06004E37);
    validate_hand_=runtime.method(utility,"ValidateUseHandCard",2,0x060043FB);
    auto footer=runtime.klass("Assembly-CSharp","Campus.InGame.Exam","ExamFooterPresenter");
    can_use_drink_=runtime.method(footer,"CanUseDrink",1,0x06005D2F);
}
json ExamAdapter::hand_validation(std::int32_t index){
    auto& runtime=*runtime_;
    auto context=runtime.invoke(create_context_,sequence_);
    if(!context)throw std::runtime_error("validator context unavailable");
    auto dispose=runtime.method(runtime.object_class(context),"Dispose",0);
    json value;
    try{
        auto result=runtime.invoke(validate_hand_,nullptr,{&index,context});
        const bool valid=runtime.field<bool>(result,"Item1");
        const auto reason=runtime.field<std::int32_t>(result,"Item2");
        if(reason<0||reason>5)throw std::runtime_error("validator result invalid");
        value={{"can_use",valid},{"reason_code",reason},{"validation_source","ExamCardUtility.ValidateUseHandCard"}};
    }catch(...){runtime.invoke(dispose,context);throw;}
    runtime.invoke(dispose,context);return value;
}
bool ExamAdapter::drink_validation(void* presenter,void* drink){
    auto& runtime=*runtime_;
    auto footer=runtime.read_object_field(presenter,"_examFooter");
    if(!footer||runtime.class_name(runtime.object_class(footer))!="ExamFooterPresenter")
        throw std::runtime_error("current Exam footer validator unavailable");
    // APK and current-PC metadata agree: this override only queries the
    // drink's ProduceEffectList.Any(); it does not open a sheet or enqueue.
    return runtime.unbox<bool>(runtime.invoke(can_use_drink_,footer,{drink}));
}
json ExamAdapter::legal_inputs(void* presenter,const json& boundary){
    auto& runtime=*runtime_;const bool ready=exam_input_boundary_ready(boundary);
    json hand=json::array(),drinks=json::array();
    const auto observed_hand=runtime.enumerate(runtime.getter(sequence_,"get_HandList"),256);
    const auto observed_drinks=runtime.enumerate(runtime.getter(runtime.getter(sequence_,"get_Parameter"),"get_DrinkList"),256);
    for(const bool card:{true,false}){
        const auto& objects=card?observed_hand:observed_drinks;
        auto& rows=card?hand:drinks;
        for(std::size_t index=0;index<objects.size();++index){
            json row={{"slot",index},{"can_use",nullptr},
                {"validation_source",card?"ExamCardUtility.ValidateUseHandCard":"ExamFooterPresenter.CanUseDrink"}};
            try{
                if(!objects[index])throw std::runtime_error("native input object is null");
                // The GUID getter lazily creates an identity for a null field.
                // Observation must not mutate the state to make it look ready.
                row[card?"card_guid":"drink_id"]=runtime.string(card?
                    runtime.read_object_field(objects[index],"_guid"):runtime.getter(objects[index],"get_Id"));
                if(ready){
                    if(card)row.update(hand_validation(static_cast<std::int32_t>(index)));
                    else row["can_use"]=drink_validation(presenter,objects[index]);
                }else row["validation_error"]="native-input-boundary-not-ready";
            }catch(const std::exception& error){
                if(!row.contains(card?"card_guid":"drink_id"))row[card?"card_guid":"drink_id"]="";
                row["validation_error"]=error.what();
            }
            rows.push_back(std::move(row));
        }
    }
    return make_exam_legal_inputs(boundary,std::move(hand),std::move(drinks));
}
json ExamAdapter::capture(const std::string& session) {
    auto& runtime=*runtime_;
    auto presenter=active_screen(runtime);
    if(runtime.class_name(runtime.object_class(presenter))!="ExamScreenPresenter")
        throw std::runtime_error("active native screen is not ExamScreenPresenter");
    if (!presenter) throw std::runtime_error("exam-presenter-unavailable");
    sequence_=runtime.invoke(presenter_method_,presenter);
    if (!sequence_) throw std::runtime_error("exam-sequence-unavailable");
    auto parameter=runtime.getter(sequence_,"get_Parameter");
    auto stack=runtime.getter(sequence_,"get_CommandStack");
    // Observe the original references before the FIRST save construction.
    // Sampling only a later save would miss an initial clone/serializer mutation.
    const auto draw_count_before=observe_total_effect_draw_count(runtime,parameter);
    json reference_presence;
    auto saved_state=save_serializer_.capture_with_presence(sequence_,parameter,reference_presence);
    json value={
        {"sequence_id",pointer_id(sequence_)},
        {"phase",runtime.unbox<std::int32_t>(runtime.getter(parameter,"get_Phase"))},
        {"busy",runtime.unbox<bool>(runtime.getter(sequence_,"get_IsCommandPlaying"))},
        {"queue_empty",runtime.unbox<bool>(runtime.getter(stack,"get_IsEmpty"))},
        {"terminal",runtime.unbox<bool>(runtime.getter(sequence_,"IsEndExam"))},
        {"turn_card_play_end",runtime.unbox<bool>(runtime.getter(parameter,"get_IsTurnCardPlayEnd"))},
        {"is_replay",runtime.unbox<bool>(runtime.getter(parameter,"get_IsReplay"))},
        {"exam_save",std::move(saved_state)},
        {"produce_context",read_produce_context(runtime)}
    };
    if(!value.at("produce_context").is_null()){
        auto& context=value["produce_context"];
        context["exam_config_bound"]=exam_context_matches(context,value.at("exam_save"));
        if(context.at("exam_config_bound")!=true)context["audition_number"]=nullptr;
    }
    value["native_legal_inputs"]=legal_inputs(presenter,value);
    try{value["exam_model_observation"]=capture_main_model_observation(runtime,sequence_,parameter,
        value.at("exam_save"),value.at("native_legal_inputs"),reference_presence,draw_count_before);}
    catch(const std::exception& error){value["exam_model_observation"]={{"schema","gkms.live-exam-model-observation.v1"},
        {"decision_type","main"},{"complete",false},{"read_errors",json::array({error.what()})}};}
    value["revision"]=sha256(session+exam_model_revision_view(value).dump());
    return value;
}
json ExamAdapter::snapshot(const std::string& session) { return capture(session); }
void* ExamAdapter::target_object(const std::string& command,const json& target) {
    if (!target.contains("slot") || !target["slot"].is_number_integer()) throw std::runtime_error("target slot required");
    const auto index=target.at("slot").get<std::int32_t>();
    auto& runtime=*runtime_;
    auto collection=command=="exam.play" ? runtime.getter(sequence_,"get_HandList") :
        runtime.getter(runtime.getter(sequence_,"get_Parameter"),"get_DrinkList");
    const auto values=runtime.enumerate(collection,256);
    if (index<0 || static_cast<std::size_t>(index)>=values.size() || !values[index]) throw std::runtime_error("target slot unavailable");
    auto object=values[index];
    const char* key=command=="exam.play" ? "card_guid" : "drink_id";
    const auto identity=runtime.string(runtime.getter(object,command=="exam.play" ? "get_Guid" : "get_Id"));
    if (identity.empty() || !target.contains(key) || !target[key].is_string() || target[key]!=identity)
        throw std::runtime_error("target identity changed");
    return object;
}
void ExamAdapter::validate(const std::string& command,const json& target,const json& before) {
    if (before.at("phase")!=6 || before.at("busy")!=false || before.at("queue_empty")!=true ||
        before.at("terminal")!=false || before.at("turn_card_play_end")!=false)
        throw std::runtime_error("exam requires settled Main boundary");
    if (before.at("is_replay")!=false) throw std::runtime_error("replay sequence is observation-only");
    if (command=="exam.end_turn") return;
    auto object=target_object(command,target);
    auto& runtime=*runtime_;
    if (command=="exam.drink") {
        if (!drink_validation(active_screen(runtime),object)) throw std::runtime_error("drink is not usable");
        return;
    }
    std::int32_t index=target.at("slot").get<std::int32_t>();
    // Use the exported invocation ABI. The ValueTuple is boxed by IL2CPP;
    // its named fields are read through the runtime, never a guessed RAX ABI.
    for (int pass=0;pass<2;++pass) {
        if(hand_validation(index).at("can_use")!=true)throw std::runtime_error("hand card is not legal");
    }
}
void ExamAdapter::submit(const std::string& command,const json& target,const json& before,json& action_receipt) {
    if (before.at("sequence_id")!=pointer_id(sequence_)) throw std::runtime_error("sequence changed before submit");
    auto& runtime=*runtime_;
    bool manual=true;
    void* action{};
    if (command=="exam.end_turn") action=runtime.invoke(end_factory_,nullptr,{&manual});
    else {
        auto object=target_object(command,target);
        std::int32_t index=target.at("slot").get<std::int32_t>();
        action=runtime.invoke(command=="exam.play" ? hand_factory_ : drink_factory_,nullptr,{&index,&manual,object});
    }
    if (!action) throw std::runtime_error("native command factory returned null");
    action_receipt["created_command_id"]=pointer_id(action);
    action_receipt["native_play_type"]=runtime.unbox<int>(runtime.getter(action,"get_PlayType"));
    action_receipt["native_play_index"]=runtime.unbox<int>(runtime.getter(action,"get_PlayIndex"));
    // This is the normal official queue entry. It remains visible to the
    // frozen recorder's AddExecuteCommand/AddPlayLog hooks.
    runtime.invoke(enqueue_,sequence_,{action});
}
} // namespace gkms::bridge
