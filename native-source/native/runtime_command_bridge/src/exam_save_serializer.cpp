#include "exam_save_serializer.hpp"

namespace gkms::bridge {
void ExamSaveSerializer::initialize(Runtime& runtime) {
    sequence_class_=runtime.klass("Assembly-CSharp","Campus.InGame.Exam","ExamSequence");
    save_class_=runtime.klass("Assembly-CSharp","Campus.InGame.Exam","ExamSaveData");
    save_ctor_=runtime.method(save_class_,".ctor",2,0x06004DE3);
    auto json_class=runtime.klass("UnityEngine.JSONSerializeModule","UnityEngine","JsonUtility");
    json_method_=runtime.method(json_class,"ToJson",2);
    runtime_=&runtime;
}

json ExamSaveSerializer::capture(void* sequence) {
    if(!runtime_)throw std::runtime_error("exam save serializer not initialized");
    auto& runtime=*runtime_;
    if(!runtime.managed_thread()||!runtime.operation_active())
        throw std::runtime_error("exam save requires a managed operation scope");
    if(!sequence||runtime.object_class(sequence)!=sequence_class_)
        throw std::runtime_error("exam save requires an ExamSequence instance");
    bool false_value=false;
    auto save=runtime.new_object(save_class_);
    runtime.invoke(save_ctor_,save,{sequence,&false_value});
    return capture_object(save);
}
json ExamSaveSerializer::capture_object(void* object) {
    if(!runtime_||!runtime_->managed_thread()||!runtime_->operation_active()||!object)
        throw std::runtime_error("object serialization requires an owned managed operation");
    bool pretty=false;
    return json::parse(runtime_->string(runtime_->invoke(json_method_,nullptr,{object,&pretty})));
}
} // namespace gkms::bridge
