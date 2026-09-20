#pragma once
#include "runtime.hpp"

namespace gkms::bridge {
// Serialize an explicitly supplied sequence on its managed owner thread.
// This does not discover a presenter, execute commands, or return managed
// pointers. The caller owns the sequence and the surrounding ManagedOperation.
class ExamSaveSerializer final {
public:
    void initialize(Runtime& runtime);
    json capture(void* sequence);
    json capture_with_presence(void* sequence,void* parameter,json& presence);
    // Same original JsonUtility path for an explicitly owned serializable
    // command/card. The caller validates its type and source pointer.
    json capture_object(void* object);
private:
    Runtime* runtime_{};
    void* sequence_class_{};
    void* save_class_{};
    void* save_ctor_{};
    void* json_method_{};
};
} // namespace gkms::bridge
