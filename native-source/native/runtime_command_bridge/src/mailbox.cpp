#include "mailbox.hpp"
#include <fstream>
#include <set>

namespace gkms::bridge {
namespace {
std::runtime_error output_error(const char* operation,const std::filesystem::path& path,DWORD error){
    // Retain the original Windows failure code before CloseHandle/other IO
    // can replace it. These diagnostics do not retry any game operation.
    return std::runtime_error(std::string(operation)+"; file="+path.filename().string()+
        "; win32="+std::to_string(error));
}
bool safe_id(const std::string& value) {
    if(value.empty() || value.size()>128) return false;
    for(const unsigned char character:value)
        if(!((character>='a'&&character<='z')||(character>='A'&&character<='Z')||(character>='0'&&character<='9')||character=='-'||character=='_')) return false;
    return true;
}
json read_request(const std::filesystem::path& path) {
    if(std::filesystem::file_size(path)>64*1024) throw std::runtime_error("request exceeds size limit");
    std::ifstream stream(path,std::ios::binary);
    if(!stream) throw std::runtime_error("request read failed");
    return json::parse(stream);
}
void move_claim(const std::filesystem::path& source,const std::filesystem::path& destination) {
    if(!MoveFileExW(source.c_str(),destination.c_str(),MOVEFILE_WRITE_THROUGH))
        throw output_error("request durable rename failed",destination,GetLastError());
}
}
void atomic_json(const std::filesystem::path& path,const json& value) {
    const auto bytes=value.dump();
    const auto temporary=std::filesystem::path(path.wstring()+L".tmp");
    const auto file=CreateFileW(temporary.c_str(),GENERIC_WRITE,0,nullptr,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,nullptr);
    if(file==INVALID_HANDLE_VALUE) throw output_error("bridge output open failed",temporary,GetLastError());
    DWORD written{};
    DWORD error=ERROR_SUCCESS;
    if(!WriteFile(file,bytes.data(),static_cast<DWORD>(bytes.size()),&written,nullptr))error=GetLastError();
    else if(written!=bytes.size())error=ERROR_WRITE_FAULT;
    else if(!FlushFileBuffers(file))error=GetLastError();
    CloseHandle(file);
    if(error!=ERROR_SUCCESS) throw output_error("bridge output flush failed",temporary,error);
    if(!MoveFileExW(temporary.c_str(),path.c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH))
        throw output_error("bridge output rename failed",path,GetLastError());
}
void validate_request(const json& request,const std::string& generation) {
    if(!request.is_object()) throw std::runtime_error("request must be object");
    const std::set<std::string> keys={"schema","request_id","session_generation","command","expected_revision","target","continuation_of"};
    for(const auto& item:request.items()) if(!keys.contains(item.key())) throw std::runtime_error("unknown request field");
    if(request.value("schema",std::string())!="gkms.runtime-command.v1") throw std::runtime_error("request schema differs");
    if(!safe_id(request.value("request_id",std::string()))) throw std::runtime_error("invalid request id");
    if(request.value("session_generation",std::string())!=generation) throw std::runtime_error("stale session generation");
    const auto command=request.value("command",std::string());
    const std::set<std::string> commands={"status","read_snapshot","read_inventory","read_model_context","read_loadout","loadout.apply","read_outer_snapshot","outer.action","exam.play","exam.drink","exam.end_turn"
    };
    if(!commands.contains(command)) throw std::runtime_error("unsupported command");
    if(request.contains("target")&&!request["target"].is_object()) throw std::runtime_error("target must be object");
    if(request.contains("continuation_of")){
        const auto parent=request.at("continuation_of").get<std::string>();
        if(command!="outer.action"||!safe_id(parent)||parent==request.at("request_id").get<std::string>()||
           request.at("target").value("exam_continuation",false)!=true||!request.at("target").contains("parent_context"))
            throw std::runtime_error("invalid Exam continuation descriptor");
    }
    if(command.starts_with("exam.")||command=="loadout.apply"||command=="outer.action"||
       command=="official_replay.prepare"||command=="official_replay.start"||command=="official_replay.release") {
        if(!request.contains("expected_revision")||!request["expected_revision"].is_string()||request["expected_revision"].get<std::string>().empty())
            throw std::runtime_error("mutating command requires exact snapshot revision");
        if(command=="exam.play"||command=="exam.drink") {
            const auto& target=request.at("target");
            if(!target.contains("slot")||!target["slot"].is_number_integer()||target["slot"].get<std::int64_t>()<0||target["slot"].get<std::int64_t>()>255)
                throw std::runtime_error("invalid native slot");
            const auto identity=target.value(command=="exam.play"?"card_guid":"drink_id",std::string());
            if(identity.empty()||identity.size()>512) throw std::runtime_error("target identity required");
        }
    }
}
void Mailbox::initialize(std::filesystem::path root) {
    root_=std::move(root);
    for(const auto* name:{"inbox","running","results","rejected"}) std::filesystem::create_directories(root_/name);
    recover();
}
void Mailbox::recover() {
    // A prior process may have entered the official command before crashing.
    // Every claimed descriptor is closed unknown; never put it back in inbox.
    for(const auto& entry:std::filesystem::directory_iterator(root_/"running")) {
        if(entry.path().extension()!=L".json") continue;
        try {
            auto request=read_request(entry.path());
            const auto id=request.value("request_id",std::string());
            if(!safe_id(id)||entry.path().stem().string()!=id) throw std::runtime_error("invalid orphan descriptor");
            const auto result=root_/"results"/(id+".json");
            if(!std::filesystem::exists(result)) atomic_json(result,{
                {"schema","gkms.runtime-command-result.v1"},{"request_id",id},
                {"session_generation",request.value("session_generation",std::string())},
                {"status","unknown"},{"error_code","process-restarted-after-claim"},
                {"error_message","A previous process claimed this command; it will not be replayed."}});
            std::filesystem::remove(entry.path());
        } catch(...) {
            move_claim(entry.path(),root_/"rejected"/(entry.path().stem().string()+"-orphan-"+std::to_string(GetTickCount64())+".json"));
        }
    }
}
std::optional<json> Mailbox::claim_next() {
    // A bounded scan: one descriptor per pump, independent of inbox size.
    for(const auto& entry:std::filesystem::directory_iterator(root_/"inbox")) {
        if(entry.path().extension()!=L".json") continue;
        const auto id=entry.path().stem().string();
        auto reject=[&]{ move_claim(entry.path(),root_/"rejected"/(id+"-"+std::to_string(GetTickCount64())+".json")); };
        if(!safe_id(id)) {reject(); return std::nullopt;}
        if(std::filesystem::file_size(entry.path())>64*1024) {reject();return std::nullopt;}
        if(std::filesystem::exists(root_/"results"/(id+".json"))||std::filesystem::exists(root_/"running"/(id+".json"))) {reject();return std::nullopt;}
        try {
            auto request=read_request(entry.path());
            if(!request.is_object()||request.value("request_id",std::string())!=id) {reject();return std::nullopt;}
            move_claim(entry.path(),root_/"running"/(id+".json"));
            return request;
        } catch(const json::exception&) {reject();return std::nullopt;}
    }
    return std::nullopt;
}
void Mailbox::finish(const json& request,const json& response) {
    const auto id=request.at("request_id").get<std::string>();
    const auto destination=root_/"results"/(id+".json");
    if(std::filesystem::exists(destination)) {
        // Result publication can succeed before running-descriptor cleanup
        // fails (for example, a reader temporarily denies FILE_SHARE_DELETE).
        // Retry only delivery of the exact copied result, never game execution
        // or replacement of a conflicting receipt.
        const auto expected=response.dump();
        if(std::filesystem::file_size(destination)!=expected.size())
            throw std::runtime_error("existing result conflicts with pending response");
        std::ifstream stream(destination,std::ios::binary);
        if(!stream)throw std::runtime_error("existing result read failed");
        const std::string actual((std::istreambuf_iterator<char>(stream)),{});
        if(stream.bad())throw std::runtime_error("existing result read failed");
        if(actual!=expected)throw std::runtime_error("existing result conflicts with pending response");
    }else atomic_json(destination,response);
    std::filesystem::remove(root_/"running"/(id+".json"));
}
void Mailbox::publish_status(const json& status) {atomic_json(root_/"status.json",status);}
json Mailbox::result_for(const std::string& id) const {
    if(!safe_id(id))throw std::runtime_error("invalid parent request id");
    return read_request(root_/"results"/(id+".json"));
}
}
