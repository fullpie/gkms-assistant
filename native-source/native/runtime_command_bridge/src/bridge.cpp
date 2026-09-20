#include "runtime.hpp"
#include "exam_adapter.hpp"
#include "exam_model_observation.hpp"
#include "pc_version_profile.hpp"
#include "inventory_adapter.hpp"
#include "mailbox.hpp"
#include "screen_context.hpp"
#include "outer_adapter.hpp"
#include "native_trace.hpp"
#include "public_runtime_paths.hpp"
#include "continuation.hpp"
#include <bcrypt.h>
#include <array>
#include <atomic>
#include <filesystem>
#include <mutex>
#include <optional>

namespace gkms::bridge {
namespace {
Runtime runtime;
ExamAdapter exam;
Mailbox mailbox;
std::filesystem::path command_root;
std::string generation;
std::string last_error;
bool initialized=false;
bool ready=false;
bool loadout_ready=false;
bool outer_ready=false;
json pc_version=json::object();
std::uint64_t unknown_event_count{};
std::string last_unknown_request_id;
std::string last_unknown_error;
ULONGLONG last_status{};
std::atomic_flag pumping=ATOMIC_FLAG_INIT;
std::optional<json> pending_request;
std::optional<json> pending_response;
std::string make_generation() {
    std::array<unsigned char,32> bytes{};
    if(BCryptGenRandom(nullptr,bytes.data(),static_cast<ULONG>(bytes.size()),BCRYPT_USE_SYSTEM_PREFERRED_RNG)<0) throw std::runtime_error("generation nonce unavailable");
    return sha256(std::string(reinterpret_cast<char*>(bytes.data()),bytes.size()));
}
json status() {
    // Feature marker for read_snapshot's native_legal_inputs payload; this is
    // not a dispatchable command. Advertise only after Exam initialization.
    auto capabilities=ready ? json::array({"status","read_snapshot","read_inventory","read_model_context","exam.play","exam.drink","exam.end_turn","exam.native_legal_inputs","exam.model_observation.v1"}) : json::array({"status"});
    if(loadout_ready){capabilities.push_back("read_loadout");capabilities.push_back("loadout.apply");}
    if(outer_ready){capabilities.push_back("read_outer_snapshot");capabilities.push_back("outer.action");capabilities.push_back("exam.continuation");}
    return {{"schema","gkms.runtime-command-status.v1"},{"protocol_version",1},
        {"pid",GetCurrentProcessId()},{"session_generation",generation},{"ready",ready},
        {"capabilities",capabilities},
        {"pc_version",pc_version},
        {"updated_at",utc_now()},{"last_error",last_error},
        {"unknown_event_count",unknown_event_count},{"last_unknown_request_id",last_unknown_request_id},{"last_unknown_error",last_unknown_error}};
}
void record_unknown(const json& request,const std::string& error){++unknown_event_count;last_unknown_request_id=request.at("request_id").get<std::string>();last_unknown_error=error;}
json execute(const json& request) {
    trace_request(request.value("request_id",std::string()),request.value("command",std::string()));
    trace_mark("command.begin");
    json response={{"schema","gkms.runtime-command-result.v1"},{"request_id",request.at("request_id")},
        {"session_generation",request.value("session_generation",std::string())},{"status","rejected"}};
    bool entered_submit=false;
    try {
        ManagedOperation lifetime(runtime);
        validate_request(request,generation);
        const auto command=request.at("command").get<std::string>();
        if(command=="status") {response["bridge_status"]=status();response["status"]="ok";return response;}
        if(!ready) throw std::runtime_error("bridge preflight not ready");
        if(command.starts_with("official_replay."))throw std::runtime_error("official replay core is disabled in this build");
        if(command=="read_diagnostic")throw std::runtime_error("read-only diagnostics disabled in this build");
        if(command=="read_snapshot") {response["snapshot"]=exam.snapshot(generation);response["status"]="ok";return response;}
        if(command=="read_model_context") {if(!ready)throw std::runtime_error("verified exam adapter required");response["model_context"]=read_live_model_context(runtime);response["status"]="ok";return response;}
        if(command=="read_inventory") {response["inventory"]=read_inventory(runtime);response["status"]="ok";return response;}
        if(command=="read_loadout") {
            if(!loadout_ready)throw std::runtime_error("loadout observer unavailable");
            response["loadout"]=read_loadout(runtime);response["status"]="ok";return response;
        }
        if(command=="read_outer_snapshot") {
            if(!outer_ready)throw std::runtime_error("outer observer unavailable");
            response["snapshot"]=read_outer_snapshot(runtime,generation);response["status"]="ok";return response;
        }
        if(command=="loadout.apply") {
            if(!loadout_ready)throw std::runtime_error("loadout observer unavailable");
            const auto outcome=apply_loadout(runtime,request.at("target"),request.at("expected_revision").get<std::string>());
            response["loadout_result"]=outcome;
            if(outcome.contains("loadout"))response["loadout"]=outcome["loadout"];
            response["applied"]=outcome.value("applied",false);
            response["applied_sections"]=outcome.value("applied_sections",json::array());
            response["full_loadout_applied"]=outcome.value("full_loadout_applied",false);
            if(outcome.value("mutation_started",false)) {
                response["status"]=outcome.value("applied",false)?"submitted":"unknown";
                if(!outcome.value("applied",false)){record_unknown(request,outcome.value("reason",std::string()));response["error_code"]="loadout-readback-unproven";response["error_message"]=outcome.value("reason",std::string());}
            } else {response["status"]="rejected";response["error_code"]=outcome.value("reason",std::string("loadout-rejected"));}
            return response;
        }
        if(command=="outer.action"){
            if(!outer_ready)throw std::runtime_error("outer observer unavailable");
            const auto before=read_outer_snapshot(runtime,generation);
            if(request.at("expected_revision")!=before.at("revision"))throw std::runtime_error("stale native outer revision");
            const auto target=request.at("target");
            if(before.value("exam_continuation",false)){
                if(!request.contains("continuation_of"))throw std::runtime_error("exam-selector-requires-parent-continuation");
                validate_exam_continuation(request,before,mailbox.result_for(request.at("continuation_of").get<std::string>()),generation);
                response["continuation_of"]=request.at("continuation_of");response["parent_context"]=before.at("parent_context");
            }else if(request.contains("continuation_of"))throw std::runtime_error("continuation requested for a non-Exam selector");
            bool member=false;
            for(const auto& action:before.at("legal_actions"))if(action.at("target")==target)member=true;
            if(!member)throw std::runtime_error("outer target absent from native choices");
            const auto stable=read_outer_snapshot(runtime,generation);
            if(stable.at("revision")!=before.at("revision"))throw std::runtime_error("outer revision changed during validation");
            entered_submit=true;
            submit_outer_action(runtime,target,before);
            response["status"]="submitted";response["settled"]=false;
            response["action"]={{"command",command},{"target",target},{"before_revision",before.at("revision")}};
            return response;
        }
        const auto before=exam.snapshot(generation);
        if(request.at("expected_revision")!=before.at("revision")) throw std::runtime_error("stale native state revision");
        const auto target=request.value("target",json::object());
        exam.validate(command,target,before);
        const auto after_validation=exam.snapshot(generation);
        if(after_validation.at("revision")!=before.at("revision")) throw std::runtime_error("native state changed during validation");
        // The durable running descriptor already exists. From this point an
        // exception is UNKNOWN, never a retryable rejection.
        response["action"]={{"command",command},{"target",target},{"sequence_id",before.at("sequence_id")},{"before_revision",before.at("revision")}};
        entered_submit=true;
        exam.submit(command,target,before,response["action"]);
        response["status"]="submitted";
        response["settled"]=false;
    } catch(const std::exception& error) {
        trace_mark("command.exception",{{"message",error.what()},{"entered_submit",entered_submit}});
        response["status"]=entered_submit?"unknown":"rejected";
        response["error_code"]=entered_submit?"native-invocation-outcome-unknown":"command-rejected";
        response["error_message"]=error.what();
        if(entered_submit)record_unknown(request,error.what());
    }
    return response;
}
}
DWORD start(const wchar_t* root) noexcept {
    if(initialized) return ready?0:2;
    try {
        if(!runtime.initialize()) return 1;
        generation=make_generation();
        command_root=root && *root ? std::filesystem::path(root) : gkms::public_runtime_paths::state_root()/L"runtime_command_bridge";
        mailbox.initialize(command_root);
        initialized=true;
        try{pc_version=observe_current_pc_version();}
        catch(const std::exception& error){pc_version={{"recognized",false},{"read_only_inspection_allowed",false},{"error",error.what()}};}
        try {
            exam.initialize(runtime);ready=true;
            pc_version["input_qualified"]=true;
            pc_version["input_qualification_scope"]="verified default method bindings; live workflow and model qualification remain separate";
        }
        catch(const std::exception& error){last_error=error.what();ready=false;}
        if(ready) {
            try{initialize_screen_context(runtime);outer_ready=true;initialize_inventory(runtime);loadout_ready=true;}
            catch(const std::exception& error){last_error=std::string("loadout initialization: ")+error.what();}
        }
        mailbox.publish_status(status());
        return ready?0:2;
    } catch(const std::exception& error){last_error=error.what();return 3;}
}
DWORD pump() noexcept {
    if(!initialized||!runtime.managed_thread()) return 1;
    if(pumping.test_and_set()) return 2;
    struct Release {~Release(){pumping.clear();}} release;
    try {
        if(!pending_request) {
            pending_request=mailbox.claim_next();
            if(pending_request){pending_response=execute(*pending_request);trace_mark("command.result_ready",{{"status",(*pending_response)["status"]}});}
        }
        if(pending_request) {
            // If publishing a result fails, retry only delivery of this copied
            // result. Never re-enter the game operation and never claim another.
            trace_mark("result.publish.before");mailbox.finish(*pending_request,*pending_response);trace_mark("result.publish.after");
            pending_request.reset();pending_response.reset();
        }
        if(GetTickCount64()-last_status>=1000) {mailbox.publish_status(status());last_status=GetTickCount64();}
        return 0;
    } catch(const std::exception& error){last_error=error.what();return 3;}
}
}
extern "C" __declspec(dllexport) unsigned int WINAPI GKMSRuntimeCommandBridgeProtocolVersion(){return 1;}
extern "C" __declspec(dllexport) DWORD WINAPI GKMSRuntimeCommandBridgeStartOnManagedThread(const wchar_t* root){return gkms::bridge::start(root);}
extern "C" __declspec(dllexport) DWORD WINAPI GKMSRuntimeCommandBridgePumpOnManagedThread(){return gkms::bridge::pump();}
BOOL APIENTRY DllMain(HMODULE module,DWORD reason,LPVOID){if(reason==DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(module);return TRUE;}
