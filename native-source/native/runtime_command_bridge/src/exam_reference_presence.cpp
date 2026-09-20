#include "exam_reference_presence.hpp"
#include "exam_save_serializer.hpp"
#include "exam_card_phase_observation.hpp"
#include "pc_method_binding.hpp"
#include "pointer_identity.hpp"
#include <memory>
#include <set>

namespace gkms::bridge {
namespace {
template<class T> T api(const char* name){
    auto module=GetModuleHandleW(L"GameAssembly.dll");
    auto value=module?GetProcAddress(module,name):nullptr;
    if(!value)throw std::runtime_error(std::string("reference presence export missing: ")+name);
    return reinterpret_cast<T>(value);
}
void require(bool valid,const char* error){if(!valid)throw std::runtime_error(error);}
void* field(Runtime& r,void* owner,const char* name){
    void* result{};
    for(auto current=r.object_class(owner);current&&!result;current=r.parent(current))
        result=api<void*(*)(void*,const char*)>("il2cpp_class_get_field_from_name")(current,name);
    require(result!=nullptr,"reference presence field unavailable");
    require(!(api<int(*)(void*)>("il2cpp_field_get_flags")(result)&0x10),"reference presence cannot read a static field");
    return result;
}
std::string type_name(void* type){
    const auto release=api<void(*)(void*)>("il2cpp_free");
    const std::unique_ptr<char,void(*)(void*)> value(api<char*(*)(void*)>("il2cpp_type_get_name")(type),release);
    require(value!=nullptr,"reference presence type name missing");return value.get();
}
void* reference(Runtime& r,void* owner,const char* name,json* evidence=nullptr){
    auto info=field(r,owner,name);
    auto type=api<void*(*)(void*)>("il2cpp_field_get_type")(info);
    require(type&&!api<bool(*)(void*)>("il2cpp_type_is_byref")(type),"reference field has no closed non-byref type");
    const int kind=api<int(*)(void*)>("il2cpp_type_get_type")(type);
    auto klass=api<void*(*)(void*)>("il2cpp_class_from_type")(type);
    require(klass&&!api<bool(*)(void*)>("il2cpp_class_is_valuetype")(klass)&&
        (kind==14||kind==18||kind==20||kind==21||kind==28||kind==29),"reference field is not a managed reference");
    void* value{};api<void(*)(void*,void*,void*)>("il2cpp_field_get_value")(owner,info,&value);
    if(evidence)*evidence={{"name",name},{"declared_type",type_name(type)},
        {"is_null",value==nullptr},{"object_id",value?json(pointer_identity(value)):json(nullptr)}};
    return value;
}
int int32_field(Runtime& r,void* owner,const char* name){
    auto info=field(r,owner,name);auto type=api<void*(*)(void*)>("il2cpp_field_get_type")(info);
    require(type&&api<int(*)(void*)>("il2cpp_type_get_type")(type)==8&&
        !api<bool(*)(void*)>("il2cpp_type_is_byref")(type),"source UID is not Int32");
    int value{};api<void(*)(void*,void*,void*)>("il2cpp_field_get_value")(owner,info,&value);return value;
}
json owner_type(Runtime& r,void* owner){
    auto klass=r.object_class(owner);auto image=api<void*(*)(void*)>("il2cpp_class_get_image")(klass);
    std::string assembly=api<const char*(*)(void*)>("il2cpp_image_get_name")(image);
    if(assembly.ends_with(".dll"))assembly.resize(assembly.size()-4);
    return {{"asm",assembly},{"ns",r.class_namespace(klass)},{"class",r.class_name(klass)}};
}
json fields(Runtime& r,void* owner,const json& serialized,std::initializer_list<const char*> names){
    json result=json::array();
    for(const auto* name:names){
        if(!serialized.contains(name))continue;
        json proof;reference(r,owner,name,&proof);result.push_back(std::move(proof));
    }
    return result;
}
}

json ExamSaveSerializer::capture_with_presence(void* sequence,void* parameter,json& presence) {
    if(!runtime_||!runtime_->managed_thread()||!runtime_->operation_active()||
        !sequence||runtime_->object_class(sequence)!=sequence_class_)
        throw std::runtime_error("presence capture requires owned ExamSequence and managed operation");
    const auto parameter_before=observe_parameter_reference_sources(*runtime_,sequence,parameter);
    const auto phases_before=observe_state_card_phase_counters(*runtime_,sequence);
    bool false_value=false;
    auto save=runtime_->new_object(save_class_);
    runtime_->invoke(save_ctor_,save,{sequence,&false_value});
    const auto result=capture_object(save);
    presence=observe_save_reference_presence(*runtime_,save,sequence,parameter,result);
    const auto parameter_after=observe_parameter_reference_sources(*runtime_,sequence,parameter);
    const auto phases_after=observe_state_card_phase_counters(*runtime_,sequence);
    presence=bind_parameter_reference_sources(std::move(presence),parameter_before,parameter_after);
    presence["card_phase_counters_before"]=phases_before;
    presence["card_phase_counters_after"]=phases_after;
    if(phases_before.at("complete")!=true||phases_after.at("complete")!=true||phases_before!=phases_after){
        presence["complete"]=false;
        presence["read_errors"].push_back("original card phase counters incomplete or changed during first serialization");
    }
    return result;
}

json observe_parameter_reference_sources(Runtime& r,void* sequence,void* parameter){
    json result={{"schema","gkms.parameter-status-reference-sources.v1"},
        {"sequence_id",pointer_identity(sequence)},{"parameter_id",pointer_identity(parameter)},
        {"active_status_sources",json::array()},{"complete",false},{"read_errors",json::array()}};
    try{
        require(r.managed_thread()&&r.operation_active(),"parameter source capture needs managed operation");
        auto collection=reference(r,parameter,"<StatusEffectCollection>k__BackingField");
        require(collection&&r.class_name(r.object_class(collection))=="ExamStatusEffectCollection"&&
            r.class_namespace(r.object_class(collection))=="Campus.InGame.Exam","parameter status collection type differs");
        result["status_collection_id"]=pointer_identity(collection);
        auto owners=r.enumerate(reference(r,collection,"_effectList"),4096);
        std::set<void*> seen;
        for(std::size_t index=0;index<owners.size();++index){
            auto owner=owners[index];require(owner&&seen.insert(owner).second,"parameter status owner is null or repeated");
            json witnesses=json::array();
            for(const auto* name:{"_triggerCard","_triggerItem","_triggerDrink","_triggerGimmickGroup"})
                if(r.has_field(r.object_class(owner),name)){
                    json proof;reference(r,owner,name,&proof);witnesses.push_back(std::move(proof));
                }
            result["active_status_sources"].push_back({{"status_index",index},
                {"owner_object_id",pointer_identity(owner)},{"owner_uid",int32_field(r,owner,"_uid")},
                {"owner_type",owner_type(r,owner)},{"fields",witnesses}});
        }
        result["complete"]=true;
    }catch(const std::exception& error){result["read_errors"].push_back(error.what());}
    return result;
}

json bind_parameter_reference_sources(json save,const json& before,const json& after){
    // Retain the exact failed observation too. Otherwise a first-serialization
    // pointer change leaves only a generic message and cannot be diagnosed.
    save["parameter_sources_before"]=before;
    save["parameter_sources_after"]=after;
    save["parameter_source_verified"]=false;
    try{
        require(save.at("complete")==true&&before.at("complete")==true&&after.at("complete")==true,
            "paired parameter/SaveData reference proof is incomplete");
        require(before==after,"parameter reference pointers changed during SaveData construction/JsonUtility");
        require(before.at("sequence_id")==save.at("sequence_id")&&before.at("parameter_id")==save.at("parameter_id"),
            "parameter reference proof belongs to another sequence/parameter");
        const auto& originals=before.at("active_status_sources");
        auto& saved=save.at("active_status_sources");
        require(saved.size()==originals.size(),"parameter and SaveData status list sizes differ");
        for(std::size_t index=0;index<saved.size();++index){
            auto& row=saved.at(index);const auto& original=originals.at(index);
            for(const auto* key:{"status_index","owner_uid","owner_type"})
                require(row.at(key)==original.at(key),"parameter and serialized status list/type/UID differ");
            require(row.at("fields").size()==original.at("fields").size(),"parameter and serialized source field coverage differs");
            for(std::size_t n=0;n<row.at("fields").size();++n)
                for(const auto* key:{"name","declared_type"})
                    require(row.at("fields").at(n).at(key)==original.at("fields").at(n).at(key),
                        "parameter and serialized source field declarations differ");
            row["save_owner_object_id"]=row.at("owner_object_id");
            row["save_fields"]=row.at("fields");
            row["owner_object_id"]=original.at("owner_object_id");
            row["fields"]=original.at("fields");
        }
        save["authority"]="actual parameter references before and after original SaveData/JsonUtility; list/type/UID joined to that exact serialized save";
        save["save_status_collection_id"]=save.at("status_collection_id");
        save["status_collection_id"]=before.at("status_collection_id");
        save["parameter_sources_before"]=before;save["parameter_sources_after"]=after;
        save["parameter_source_verified"]=true;
    }catch(const std::exception& error){save["complete"]=false;save["parameter_source_verified"]=false;save["read_errors"].push_back(error.what());}
    return save;
}

json observe_save_reference_presence(Runtime& r,void* save,void* sequence,void* parameter,const json& serialized){
    json result={{"schema","gkms.live-exam-reference-presence.v1"},
        {"sequence_id",pointer_identity(sequence)},{"parameter_id",pointer_identity(parameter)},
        {"save_object_id",pointer_identity(save)},{"state_native_sha256",sha256(serialized.dump())},
        {"authority","actual reference fields on the same original ExamSaveData used for JsonUtility"},
        {"active_status_sources",json::array()},{"read_errors",json::array()},{"complete",false}};
    try{
        require(r.managed_thread()&&r.operation_active(),"reference presence needs managed operation");
        require(r.class_name(r.object_class(save))=="ExamSaveData"&&
            r.class_namespace(r.object_class(save))=="Campus.InGame.Exam","reference presence save type differs");
        auto collection=reference(r,save,"status");
        require(collection&&r.class_name(r.object_class(collection))=="ExamStatusEffectCollection"&&
            r.class_namespace(r.object_class(collection))=="Campus.InGame.Exam","status collection type differs");
        auto list=reference(r,collection,"_effectList");
        auto owners=r.enumerate(list,4096);
        const auto& status=serialized.at("status").at("_effectList");
        const auto& refs=serialized.at("references").at("RefIds");
        require(status.is_array()&&refs.is_array()&&status.size()==owners.size(),"serialized status membership differs");
        result["status_collection_id"]=pointer_identity(collection);
        std::set<void*> seen;
        for(std::size_t index=0;index<owners.size();++index){
            auto owner=owners[index];require(owner&&seen.insert(owner).second,"status owner is null or repeated");
            const auto& member=status.at(index);require(member.is_object()&&member.size()==1&&member.contains("rid"),"status membership lacks exact rid");
            const auto rid=member.at("rid");require(rid.is_number_integer(),"status rid is not an integer");
            const json* found=nullptr;std::size_t reference_index=0;
            for(std::size_t n=0;n<refs.size();++n)if(refs.at(n).at("rid")==rid){
                require(!found,"status rid is ambiguous");found=&refs.at(n);reference_index=n;}
            require(found&&found->at("data").is_object(),"status rid has no data row");
            const auto type=owner_type(r,owner);const auto uid=int32_field(r,owner,"_uid");
            require(found->at("type")==type&&found->at("data").at("_uid")==uid,
                "native status owner differs from serialized rid/type/UID");
            result["active_status_sources"].push_back({{"status_index",index},{"rid",rid},
                {"reference_index",reference_index},{"owner_object_id",pointer_identity(owner)},
                {"owner_type",type},{"owner_uid",uid},{"fields",fields(r,owner,found->at("data"),
                    {"_triggerCard","_triggerItem","_triggerDrink","_triggerGimmickGroup"})}});
        }
        result["complete"]=true;
    }catch(const std::exception& error){result["read_errors"].push_back(error.what());}
    return result;
}

json observe_command_reference_presence(Runtime& r,void* command,const json& serialized){
    json result={{"schema","gkms.live-exam-command-presence.v1"},{"command_id",pointer_identity(command)},
        {"command_native_sha256",sha256(serialized.dump())},{"complete",false},{"read_errors",json::array()}};
    try{
        require(r.managed_thread()&&r.operation_active(),"command presence needs managed operation");
        require(r.class_name(r.object_class(command))=="ExamPlayCommand"&&
            r.class_namespace(r.object_class(command))=="Campus.InGame.Exam","command presence type differs");
        result["fields"]=fields(r,command,serialized,{"_playingCard","_playingDrink","_playingItem","_playingGimmick"});
        result["card_phase_counters"]=observe_command_card_phase_counters(r,command);
        require(result.at("card_phase_counters").at("complete")==true,"command card phase counter capture incomplete");
        result["complete"]=true;
    }catch(const std::exception& error){result["read_errors"].push_back(error.what());}
    return result;
}

int observe_total_effect_draw_count(Runtime& r,void* parameter){
    require(r.managed_thread()&&r.operation_active(),"live counter read needs managed operation");
    auto klass=r.object_class(parameter);
    require(r.class_name(klass)=="ExamParameterModel"&&r.class_namespace(klass)=="Campus.InGame.Exam","counter parameter owner differs");
    auto method=r.method(klass,"get_TotalEffectDrawCardCount",0);
    const auto identity=verified_pc_binding_identity();
    std::uint32_t token{};
    if(identity.at("metadata_sha256")=="9349bc965fb08a434ab1c9547a3440a8ee02a05e761723792aabe5f4a9ecb635")token=0x06004CDFu;
    else if(identity.at("metadata_sha256")=="9a6bf153c0c42a2768e619cc7d96fa79fcc1d341e9cf9bcbdc8d6bca48812668")token=0x06004CD4u;
    else throw std::runtime_error("live counter consumer has no exact typed getter contract");
    std::uint32_t implementation{};
    auto returned=api<void*(*)(void*)>("il2cpp_method_get_return_type")(method);
    require(api<std::uint32_t(*)(void*)>("il2cpp_method_get_token")(method)==token&&
        api<void*(*)(void*)>("il2cpp_method_get_class")(method)==klass&&
        !(api<std::uint32_t(*)(void*,std::uint32_t*)>("il2cpp_method_get_flags")(method,&implementation)&0x10)&&
        api<int(*)(void*)>("il2cpp_type_get_type")(returned)==8&&
        !api<bool(*)(void*)>("il2cpp_type_is_byref")(returned),"current counter getter differs from its typed source contract");
    auto boxed=r.invoke(method,parameter);
    require(boxed&&r.class_name(r.object_class(boxed))=="Int32"&&r.class_namespace(r.object_class(boxed))=="System",
        "counter getter did not return boxed Int32");
    return r.unbox<int>(boxed);
}
}
