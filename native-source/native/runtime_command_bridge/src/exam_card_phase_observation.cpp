#include "exam_card_phase_observation.hpp"
#include "nullable_int32_storage.hpp"
#include "pointer_identity.hpp"
#include <array>
#include <memory>
#include <set>

namespace gkms::bridge {
namespace {
template<class T> T api(const char* name){
    const auto module=GetModuleHandleW(L"GameAssembly.dll");
    const auto address=module?GetProcAddress(module,name):nullptr;
    if(!address)throw std::runtime_error(std::string("card phase export missing: ")+name);
    return reinterpret_cast<T>(address);
}
void require(bool valid,const char* error){if(!valid)throw std::runtime_error(error);}
std::string type_name(void* type){
    const auto release=api<void(*)(void*)>("il2cpp_free");
    const std::unique_ptr<char,void(*)(void*)> text(api<char*(*)(void*)>("il2cpp_type_get_name")(type),release);
    require(text!=nullptr,"card phase field type name unavailable");return text.get();
}
void* field(Runtime& r,void* klass,const char* name){
    void* result{};
    for(auto owner=klass;owner&&!result;owner=r.parent(owner))
        result=api<void*(*)(void*,const char*)>("il2cpp_class_get_field_from_name")(owner,name);
    require(result!=nullptr,"card phase field unavailable");
    require(!(api<int(*)(void*)>("il2cpp_field_get_flags")(result)&0x10),"card phase field is static");
    const auto type=api<void*(*)(void*)>("il2cpp_field_get_type")(result);
    require(type&&!api<bool(*)(void*)>("il2cpp_type_is_byref")(type),"card phase field has unsupported byref type");
    return result;
}
void* reference(Runtime& r,void* owner,const char* name){
    require(owner!=nullptr,"card phase reference owner is null");
    const auto info=field(r,r.object_class(owner),name);
    const auto type=api<void*(*)(void*)>("il2cpp_field_get_type")(info);
    const auto klass=api<void*(*)(void*)>("il2cpp_class_from_type")(type);
    const auto kind=api<int(*)(void*)>("il2cpp_type_get_type")(type);
    require(klass&&!api<bool(*)(void*)>("il2cpp_class_is_valuetype")(klass)&&
        (kind==14||kind==18||kind==20||kind==21||kind==28||kind==29),"card phase field is not a managed reference");
    void* value{};api<void(*)(void*,void*,void*)>("il2cpp_field_get_value")(owner,info,&value);return value;
}
std::int32_t number(Runtime& r,void* owner,const char* name,bool phase_enum=false){
    const auto info=field(r,r.object_class(owner),name);
    const auto type=api<void*(*)(void*)>("il2cpp_field_get_type")(info);
    if(phase_enum){
        const auto klass=api<void*(*)(void*)>("il2cpp_class_from_type")(type);
        require(type_name(type)=="Campus.Common.Proto.Client.Enums.ProduceExamPhaseType"&&klass&&
            api<bool(*)(void*)>("il2cpp_class_is_enum")(klass),"phase key is not the expected enum");
        require(api<int(*)(void*)>("il2cpp_type_get_type")(
            api<void*(*)(void*)>("il2cpp_class_enum_basetype")(klass))==8,"phase enum is not Int32");
    }else require(api<int(*)(void*)>("il2cpp_type_get_type")(type)==8,"card phase counter is not Int32");
    std::int32_t value{};api<void(*)(void*,void*,void*)>("il2cpp_field_get_value")(owner,info,&value);return value;
}
void expect(Runtime& r,void* object,const char* ns,const char* name){
    require(object&&r.class_namespace(r.object_class(object))==ns&&r.class_name(r.object_class(object))==name,
        "card phase owner type differs");
}
json nullable(Runtime& r,void* phase,json& layout){
    const auto info=field(r,r.object_class(phase),"forecastStart");
    const auto type=api<void*(*)(void*)>("il2cpp_field_get_type")(info);
    require(type_name(type)=="System.Nullable<System.Int32>","forecastStart is not closed Nullable<Int32>");
    const auto klass=api<void*(*)(void*)>("il2cpp_class_from_type")(type);
    require(klass&&api<bool(*)(void*)>("il2cpp_class_is_valuetype")(klass),"nullable storage is not a value type");
    std::set<std::string> names;void* iterator{};
    while(auto member=api<void*(*)(void*,void**)>("il2cpp_class_get_fields")(klass,&iterator)){
        require(!(api<int(*)(void*)>("il2cpp_field_get_flags")(member)&0x10),"unexpected static Nullable member");
        names.insert(api<const char*(*)(void*)>("il2cpp_field_get_name")(member));
    }
    require(names==std::set<std::string>{"hasValue","value"},"nullable storage declaration differs");
    const auto has=field(r,klass,"hasValue"),value=field(r,klass,"value");
    require(api<int(*)(void*)>("il2cpp_type_get_type")(api<void*(*)(void*)>("il2cpp_field_get_type")(has))==2&&
        api<int(*)(void*)>("il2cpp_type_get_type")(api<void*(*)(void*)>("il2cpp_field_get_type")(value))==8,
        "nullable storage members are not Boolean/Int32");
    std::uint32_t alignment{};
    const auto size=api<std::int32_t(*)(void*,std::uint32_t*)>("il2cpp_class_value_size")(klass,&alignment);
    require(size>0&&size<=64,"nullable storage size exceeds boundary");
    const auto header=api<std::uint32_t(*)()>("il2cpp_object_header_size")();
    const auto has_offset=api<std::size_t(*)(void*)>("il2cpp_field_get_offset")(has);
    const auto value_offset=api<std::size_t(*)(void*)>("il2cpp_field_get_offset")(value);
    std::vector<unsigned char> bytes(static_cast<std::size_t>(size));
    api<void(*)(void*,void*,void*)>("il2cpp_field_get_value")(phase,info,bytes.data());
    layout={{"type","System.Nullable<System.Int32>"},{"value_size",size},{"header_size",header},
        {"has_value_offset",has_offset},{"value_offset",value_offset}};
    return decode_nullable_int32_storage(bytes,header,has_offset,value_offset);
}
json phase_entries(Runtime& r,void* dictionary){
    json result=json::array();
    if(!dictionary)return result;
    const auto list=reference(r,dictionary,"_list");
    if(!list)return result;
    const auto entries=r.enumerate(list,4096);std::set<std::int32_t> keys;
    for(std::size_t i=0;i<entries.size();++i){
        auto pair=entries[i];require(pair!=nullptr,"phase dictionary entry is null");
        const auto key=number(r,pair,"key",true);require(keys.insert(key).second,"phase dictionary key is repeated");
        const auto phase=reference(r,pair,"value");expect(r,phase,"Campus.InGame.Exam","PhaseCountData");
        json layout;const auto forecast=nullable(r,phase,layout);
        result.push_back({{"ordinal",i},{"key",key},{"current",number(r,phase,"current")},
            {"phase_object_id",pointer_identity(phase)},{"forecastStart",forecast},{"nullable_storage",layout}});
    }
    return result;
}
json card(Runtime& r,void* object,const char* zone,std::size_t index){
    expect(r,object,"Campus.InGame.Card","ExamCardData");
    const auto guid=reference(r,object,"_guid"),data=reference(r,object,"_cardData");
    require(data!=nullptr,"phase card definition object is absent");
    auto status=reference(r,object,"_statusEffect");
    json row={{"zone",zone},{"index",index},{"card_object_id",pointer_identity(object)},
        {"card_guid",guid?json(r.string(guid)):json(nullptr)},
        {"card_id",r.string(reference(r,data,"_id"))},{"card_upgrade",number(r,data,"_upgradeCount")},
        {"status_object_id",status?json(pointer_identity(status)):json(nullptr)},
        {"status_id",nullptr},{"entries",json::array()}};
    if(!status)return row;
    expect(r,status,"Campus.InGame.Card","ProduceCardStatusEffect");
    row["status_id"]=r.string(reference(r,status,"_id"));
    row["entries"]=phase_entries(r,reference(r,status,"_phaseCountDictionary"));
    return row;
}
json base(void* owner,const char* scope){return {{"schema","gkms.live-card-phase-counters.v1"},
    {"scope",scope},{"owner_id",pointer_identity(owner)},{"complete",false},
    {"read_errors",json::array()},{"cards",json::array()},{"active_statuses",json::array()}};}
}
json observe_state_card_phase_counters(Runtime& r,void* sequence){
    auto result=base(sequence,"state");
    try{
        require(r.managed_thread()&&r.operation_active(),"phase capture needs managed operation");
        expect(r,sequence,"Campus.InGame.Exam","ExamSequence");
        for(const auto& [zone,name]:std::array<std::pair<const char*,const char*>,5>{{
            {"handList","_hand"},{"deckList","_deck"},{"graveList","_grave"},{"lostList","_lost"},{"holdList","_hold"}}}){
            auto pool=reference(r,sequence,name);require(pool!=nullptr,"original card zone is null");
            for(int depth=0;r.has_field(r.object_class(pool),"_cardList");++depth){
                require(depth<4,"card zone wrapper nesting exceeds boundary");
                pool=reference(r,pool,"_cardList");require(pool!=nullptr,"card zone list is null");
            }
            const auto cards=r.enumerate(pool,4096);
            for(std::size_t i=0;i<cards.size();++i)result["cards"].push_back(card(r,cards[i],zone,i));
        }
        const auto parameter=reference(r,sequence,"<Parameter>k__BackingField");
        const auto collection=reference(r,parameter,"<StatusEffectCollection>k__BackingField");
        require(collection!=nullptr,"active status collection is null");
        const auto statuses=r.enumerate(reference(r,collection,"_effectList"),4096);
        for(std::size_t i=0;i<statuses.size();++i){
            const auto status=statuses[i];require(status!=nullptr,"active status owner is null");
            const auto klass=r.object_class(status);
            const auto image=api<void*(*)(void*)>("il2cpp_class_get_image")(klass);
            std::string assembly=api<const char*(*)(void*)>("il2cpp_image_get_name")(image);
            if(assembly.ends_with(".dll"))assembly.resize(assembly.size()-4);
            const bool has=r.has_field(klass,"_phaseCountDictionary");
            result["active_statuses"].push_back({{"status_index",i},{"owner_object_id",pointer_identity(status)},
                {"owner_uid",number(r,status,"_uid")},
                {"owner_type",{{"asm",assembly},{"ns",r.class_namespace(klass)},{"class",r.class_name(klass)}}},
                {"has_phase_count_dictionary",has},
                {"entries",has?phase_entries(r,reference(r,status,"_phaseCountDictionary")):json::array()}});
        }
        result["complete"]=true;
    }catch(const std::exception& error){result["read_errors"].push_back(error.what());}
    return result;
}
json observe_command_card_phase_counters(Runtime& r,void* command){
    auto result=base(command,"command");
    try{
        require(r.managed_thread()&&r.operation_active(),"command phase capture needs managed operation");
        expect(r,command,"Campus.InGame.Exam","ExamPlayCommand");
        const auto object=reference(r,command,"_playingCard");
        if(object)result["cards"].push_back(card(r,object,"command._playingCard",0));
        result["complete"]=true;
    }catch(const std::exception& error){result["read_errors"].push_back(error.what());}
    return result;
}
}
