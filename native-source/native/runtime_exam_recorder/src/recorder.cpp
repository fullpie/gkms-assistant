#include "recorder.hpp"
#include "validate_use_hand_card_abi.hpp"

#include "il2cpp_api.hpp"
#include "pc_method_binding.hpp"
#include "recorder_field_contract.hpp"
#include "public_runtime_paths.hpp"

#include <MinHook.h>
#include <Psapi.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <locale>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <thread>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <vector>

namespace gkms::runtime_exam_recorder {
namespace {

using gkms::il2cpp::Api;
using gkms::il2cpp::FieldInfo;
using gkms::il2cpp::Il2CppString;
using gkms::il2cpp::MethodInfo;

constexpr const char* kAssembly = "Assembly-CSharp";
constexpr const char* kExamNamespace = "Campus.InGame.Exam";
constexpr const char* kJsonAssembly = "UnityEngine.JSONSerializeModule";
constexpr const char* kJsonNamespace = "UnityEngine";
constexpr std::uint32_t kProtocolVersion = 1;

// This candidate is deliberately pinned to the currently retained PC pair.
// A mismatch is a hard preflight failure; no subset of hooks is enabled.
constexpr DWORD kExpectedGameAssemblyImageSize = 0x0BE0E000;
constexpr DWORD kExpectedGameAssemblyTimestamp = 0x6A73E78D;

constexpr std::size_t kMaximumQueuedEvents = 16384;
constexpr std::size_t kMaximumPendingActionsPerSequence = 256;
constexpr std::size_t kMaximumPendingGeneratedContexts = 256;
constexpr std::size_t kMaximumCommandIdentityEntries = 4096;
constexpr std::size_t kMaximumJsonBytes = 16U * 1024U * 1024U;
constexpr const char* kExpectedMetadataSha256 =
    "9A6BF153C0C42A2768E619CC7D96FA79FCC1D341E9CF9BCBDC8D6BCA48812668";
constexpr std::size_t kMaximumEffectCardSelectIndexes = 64;
constexpr const char* kCapturedActionSchema =
    "gkms.runtime-captured-action.v2";
constexpr const char* kActionSettlementSchema =
    "gkms.runtime-action-settlement.v1";
constexpr int kMainPhase = 6;

#if defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || \
    defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE)
// The candidate target is pinned to the same current PC metadata pair as the
// recorder.  The hash is evidence in the emitted preflight row; method
// token/arity/executable-pointer checks below are the runtime gate.
constexpr std::size_t kMaximumCandidateListItems = 256;
constexpr std::size_t kMaximumCandidateBlockers = 32;
#endif

HMODULE g_module{};
Api g_api;
nlohmann::json g_verified_pc_identity = nlohmann::json::object();
std::ofstream g_output;
std::mutex g_output_mutex;
std::condition_variable g_output_ready;
std::deque<std::string> g_output_queue;
HANDLE g_writer_thread{};
std::atomic<bool> g_writer_running{false};
std::atomic<bool> g_writer_stopping{false};
std::atomic<std::uint64_t> g_sequence{0};
std::atomic<std::uint64_t> g_dropped_events{0};
std::atomic<std::uint32_t> g_active_hooks{0};
std::atomic<bool> g_stopping{false};
bool g_minhook_owner{};

enum class StartState : std::uint32_t {
    idle,
    running,
    ready,
    failed,
};
std::atomic<StartState> g_start_state{StartState::idle};

thread_local std::uint32_t g_hook_mask{};
thread_local std::uint32_t g_snapshot_depth{};
thread_local std::uint32_t g_terminal_marker_depth{};

class InvariantStream final : public std::ostringstream {
public:
    InvariantStream() {
        imbue(std::locale::classic());
    }
};

std::string json_escape(const std::string& value) {
    InvariantStream out;
    for (const unsigned char character : value) {
        switch (character) {
        case '"': out << "\\\""; break;
        case '\\': out << "\\\\"; break;
        case '\b': out << "\\b"; break;
        case '\f': out << "\\f"; break;
        case '\n': out << "\\n"; break;
        case '\r': out << "\\r"; break;
        case '\t': out << "\\t"; break;
        default:
            if (character < 0x20) {
                out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                    << static_cast<int>(character) << std::dec;
            } else {
                out << character;
            }
        }
    }
    return out.str();
}

std::string pointer_json(const void* value) {
    InvariantStream out;
    out << "\"0x" << std::hex << reinterpret_cast<std::uintptr_t>(value) << "\"";
    return out.str();
}

std::string mh_status_json(MH_STATUS status) {
    return json_escape(MH_StatusToString(status));
}

void bootstrap_marker(const char* stage) noexcept {
    std::filesystem::path path;
    try {
        const auto directory = gkms::public_runtime_paths::state_root() / L"telemetry";
        std::filesystem::create_directories(directory);
        path = directory / (L"runtime_exam_recorder_bootstrap_" +
            std::to_wstring(GetCurrentProcessId()) + L".log");
    } catch (...) {
        return;
    }
    HANDLE file = CreateFileW(
        path.c_str(),
        FILE_APPEND_DATA,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        return;
    }
    char line[256]{};
    const int length = std::snprintf(
        line,
        sizeof(line),
        "%llu thread=%lu %s\r\n",
        static_cast<unsigned long long>(GetTickCount64()),
        GetCurrentThreadId(),
        stage == nullptr ? "unknown" : stage);
    if (length > 0) {
        DWORD written{};
        WriteFile(file, line, static_cast<DWORD>(length), &written, nullptr);
        FlushFileBuffers(file);
    }
    CloseHandle(file);
}

DWORD WINAPI writer_worker(void*) {
    for (;;) {
        std::string line;
        {
            std::unique_lock lock(g_output_mutex);
            g_output_ready.wait(lock, [] {
                return g_writer_stopping.load() || !g_output_queue.empty();
            });
            if (g_output_queue.empty() && g_writer_stopping.load()) {
                break;
            }
            line = std::move(g_output_queue.front());
            g_output_queue.pop_front();
        }
        // The writer receives only an already-copied native string.  No
        // IL2CPP object or managed pointer is ever dereferenced here.
        g_output << line;
        g_output.flush();
    }
    return 0;
}

bool start_writer() {
    g_writer_stopping = false;
    g_writer_running = true;
    g_writer_thread = CreateThread(nullptr, 0, &writer_worker, nullptr, 0, nullptr);
    if (g_writer_thread == nullptr) {
        g_writer_running = false;
        return false;
    }
    return true;
}

void stop_writer() {
    if (!g_writer_running.exchange(false)) {
        return;
    }
    g_writer_stopping = true;
    g_output_ready.notify_all();
    if (g_writer_thread != nullptr) {
        WaitForSingleObject(g_writer_thread, 5000);
        CloseHandle(g_writer_thread);
        g_writer_thread = nullptr;
    }
}

void enqueue_line(std::string line) noexcept {
    if (!g_writer_running.load()) {
        return;
    }
    try {
        {
            std::lock_guard lock(g_output_mutex);
            if (g_output_queue.size() >= kMaximumQueuedEvents) {
                ++g_dropped_events;
                return;
            }
            g_output_queue.push_back(std::move(line));
        }
        g_output_ready.notify_one();
    } catch (...) {
        ++g_dropped_events;
    }
}

void emit_line(
    const std::string& body,
    bool exact = false,
    bool legal_actions_complete = false) noexcept {
    try {
        InvariantStream line;
        line << "{\"schema\":\"gkms.runtime-exam-recorder.shadow.v1\""
            << ",\"sequence\":" << ++g_sequence
            << ",\"pid\":" << GetCurrentProcessId()
            << ",\"thread_id\":" << GetCurrentThreadId()
            << ",\"shadow\":true"
            << ",\"exact\":" << (exact ? "true" : "false")
            << ",\"legal_actions_complete\":"
            << (legal_actions_complete ? "true" : "false")
            << ",\"body\":" << body << "}\n";
        enqueue_line(line.str());
    } catch (...) {
        ++g_dropped_events;
    }
}

void emit_error(const char* reason) noexcept {
    try {
        emit_line(
            std::string("{\"record\":\"recorder_error\",\"reason\":\"") +
            json_escape(reason == nullptr ? "unknown" : reason) + "\"}");
    } catch (...) {
    }
}

// Profile lookup can throw ordinary C++ exceptions. Keep it outside the
// compiler's SEH-only invocation helpers and fail closed before any detour.
std::uint32_t source_binding_token(void* klass, const char* name,
    std::uint32_t arity, std::uint32_t source_token) noexcept {
    try {
        return gkms::bridge::pc_binding_token(klass, name,
            static_cast<int>(arity), source_token);
    } catch (const std::exception& error) {
        emit_error(error.what());
    } catch (...) {
        emit_error("pc-method-profile-binding-failed");
    }
    return 0;
}

std::uint32_t method_binding_token(MethodInfo* method,
    std::uint32_t arity, std::uint32_t source_token) noexcept {
    if (!method) return 0;
    const auto owner = reinterpret_cast<void* (*)(void*)>(
        GetProcAddress(g_api.game_assembly(), "il2cpp_method_get_class"));
    const auto name = reinterpret_cast<const char* (*)(void*)>(
        GetProcAddress(g_api.game_assembly(), "il2cpp_method_get_name"));
    if (!owner || !name) return 0;
    return source_binding_token(owner(method), name(method), arity, source_token);
}

struct RecorderFieldApi {
    void* (*field_type)(void*){};
    void* (*field_parent)(void*){};
    std::size_t (*field_offset)(void*){};
    int (*field_flags)(void*){};
    void (*get_value)(void*, void*, void*){};
    int (*type_kind)(void*){};
    bool (*type_byref)(void*){};
    void* (*class_from_type)(void*){};
    bool (*is_value_type)(void*){};
    bool (*is_enum)(void*){};
    void* (*enum_basetype)(void*){};
    std::int32_t (*value_size)(void*, std::uint32_t*){};
    std::int32_t (*instance_size)(void*){};
    void* (*object_class)(void*){};
    bool (*is_assignable_from)(void*, void*){};

    bool initialize(HMODULE module) noexcept {
#define FIELD_EXPORT(member, name) member = reinterpret_cast<decltype(member)>(GetProcAddress(module, name)); if (!member) return false
        FIELD_EXPORT(field_type, "il2cpp_field_get_type");
        FIELD_EXPORT(field_parent, "il2cpp_field_get_parent");
        FIELD_EXPORT(field_offset, "il2cpp_field_get_offset");
        FIELD_EXPORT(field_flags, "il2cpp_field_get_flags");
        FIELD_EXPORT(get_value, "il2cpp_field_get_value");
        FIELD_EXPORT(type_kind, "il2cpp_type_get_type");
        FIELD_EXPORT(type_byref, "il2cpp_type_is_byref");
        FIELD_EXPORT(class_from_type, "il2cpp_class_from_type");
        FIELD_EXPORT(is_value_type, "il2cpp_class_is_valuetype");
        FIELD_EXPORT(is_enum, "il2cpp_class_is_enum");
        FIELD_EXPORT(enum_basetype, "il2cpp_class_enum_basetype");
        FIELD_EXPORT(value_size, "il2cpp_class_value_size");
        FIELD_EXPORT(instance_size, "il2cpp_class_instance_size");
        FIELD_EXPORT(object_class, "il2cpp_object_get_class");
        FIELD_EXPORT(is_assignable_from, "il2cpp_class_is_assignable_from");
#undef FIELD_EXPORT
        return true;
    }
};
RecorderFieldApi g_field_api;

bool recorder_field_shape(FieldInfo* field, RecorderFieldShape& shape) noexcept {
    if (!field || !g_field_api.field_type) return false;
    __try {
        auto type = g_field_api.field_type(field);
        auto parent = g_field_api.field_parent(field);
        auto klass = type ? g_field_api.class_from_type(type) : nullptr;
        if (!type || !parent || !klass) return false;
        shape.kind = g_field_api.type_kind(type);
        shape.byref = g_field_api.type_byref(type);
        shape.is_static = (g_field_api.field_flags(field) & 0x10) != 0;
        shape.is_value_type = g_field_api.is_value_type(klass);
        shape.is_enum = g_field_api.is_enum(klass);
        shape.offset = g_field_api.field_offset(field);
        const auto parent_size = g_field_api.instance_size(parent);
        if (parent_size <= 0) return false;
        shape.parent_instance_size = static_cast<std::size_t>(parent_size);
        if (shape.is_value_type) {
            std::uint32_t alignment{};
            const auto size = g_field_api.value_size(klass, &alignment);
            if (size <= 0) return false;
            shape.value_size = static_cast<std::size_t>(size);
            if (shape.is_enum) {
                auto underlying = g_field_api.enum_basetype(klass);
                if (!underlying) return false;
                shape.enum_underlying_kind = g_field_api.type_kind(underlying);
            }
        } else {
            shape.value_size = sizeof(void*);
        }
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

template<class T> bool recorder_field_usable(FieldInfo* field) noexcept {
    static_assert(std::is_pointer_v<T> || std::is_same_v<T, std::int32_t>);
    RecorderFieldShape shape{};
    return recorder_field_shape(field, shape) &&
        recorder_field_copy_allowed(shape, sizeof(T), std::is_pointer_v<T>);
}

bool copy_recorder_field(void* instance, FieldInfo* field, void* destination,
    std::size_t size, bool reference) noexcept {
    RecorderFieldShape shape{};
    if (!instance || !destination || !recorder_field_shape(field, shape) ||
        !recorder_field_copy_allowed(shape, size, reference)) return false;
    __try {
        auto parent = g_field_api.field_parent(field);
        auto actual = g_field_api.object_class(instance);
        if (!parent || !actual || !g_field_api.is_assignable_from(parent, actual)) return false;
        g_field_api.get_value(instance, field, destination);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

template<class T> bool recorder_read_field(void* instance, FieldInfo* field, T& result) noexcept {
    static_assert(std::is_pointer_v<T> || std::is_same_v<T, std::int32_t>);
    T value{};
    if (!copy_recorder_field(instance, field, &value, sizeof(T), std::is_pointer_v<T>)) return false;
    result = value;
    return true;
}

std::string recorder_field_diagnostic(FieldInfo* field) {
    RecorderFieldShape shape{};
    if (!recorder_field_shape(field, shape)) return "null";
    return nlohmann::json{{"offset",shape.offset},{"value_size",shape.value_size},
        {"kind",shape.kind},{"offset_source","il2cpp_field_get_offset"}}.dump();
}

struct Bindings {
    void* sequence_class{};
    void* parameter_class{};
    void* play_command_class{};
    void* save_class{};
    void* json_class{};
    // Generated-card identity is resolved from the same loaded metadata as
    // the action recorder.  Keep these classes/methods separate from the
    // snapshot path so a failed generated-card probe can never fall back to
    // a post-state diff or a guessed GUID.
    void* effect_context_class{};
    void* card_class{};
    void* card_move_controller_class{};
    void* card_create_id_class{};
    void* command_stack_class{};

    MethodInfo* create_use_hand{};
    MethodInfo* create_use_drink{};
    MethodInfo* create_turn_end{};
    MethodInfo* add_execute_command{};
    MethodInfo* set_is_command_playing{};
    MethodInfo* sequence_parameter{};
    MethodInfo* is_end_exam{};
    MethodInfo* is_exam_end_complete{};
    MethodInfo* set_exam_end_complete{};
    MethodInfo* exam_save_ctor{};
    MethodInfo* json_to_json{};
    void* object_new{};
    void* exam_play_log_class{};
    MethodInfo* main_action_log_ctor{};
    MethodInfo* effect_card_select_log_ctor{};
    MethodInfo* add_play_log{};
    MethodInfo* play_log_is_select{};
    MethodInfo* play_log_select_indexes{};
    MethodInfo* play_log_command{};

    // These five getters are part of the shared action-settlement spine, not
    // only the optional legal-action probe.  The post-original busy=false
    // hook must prove that the official command queue really drained before
    // it may publish physical settlement or a decision state.
    MethodInfo* candidate_parameter_phase{};
    MethodInfo* candidate_sequence_command_playing{};
    MethodInfo* candidate_parameter_turn_card_play_end{};
    MethodInfo* candidate_sequence_command_stack{};
    MethodInfo* candidate_command_stack_is_empty{};

    MethodInfo* generated_context_parameter{};
    MethodInfo* generated_context_playing_card{};
    MethodInfo* generated_context_playing_effect{};
    MethodInfo* generated_context_random_int{};
    MethodInfo* generated_parameter_random_state{};
    MethodInfo* generated_create_id_execute{};
    MethodInfo* generated_create_id_move_position{};
    MethodInfo* generated_card_guid{};
    MethodInfo* generated_card_id{};
    MethodInfo* generated_card_upgrade{};
    MethodInfo* generated_card_create_guid{};
    MethodInfo* generated_add_card_single{};
    MethodInfo* generated_add_card_list{};
    FieldInfo* generated_card_guid_field{};
    FieldInfo* generated_create_id_target_card{};
    FieldInfo* generated_create_id_target_upgrade{};
    FieldInfo* generated_create_id_move_position_field{};
#if defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || \
    defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE)
    void* card_utility_class{};
    void* drink_class{};
    MethodInfo* candidate_sequence_hand_list{};
    MethodInfo* candidate_create_effect_resolver{};
    MethodInfo* candidate_context_hand_list{};
    MethodInfo* candidate_validate_use_hand_card{};
    MethodInfo* candidate_context_dispose{};
    MethodInfo* candidate_parameter_drink_list{};
    MethodInfo* candidate_drink_effect_list{};
    MethodInfo* candidate_drink_id{};
    MethodInfo* candidate_list_count{};
    MethodInfo* candidate_list_item{};
#endif
};
Bindings g_bindings;

// Static factory and instance boundary ABIs.  These declarations are used
// only after runtime token/arity/executable-pointer checks have succeeded.
using create_use_hand_fn = void* (*)(int, bool, void*, const MethodInfo*);
using create_use_drink_fn = void* (*)(int, bool, void*, const MethodInfo*);
using create_turn_end_fn = void* (*)(bool, const MethodInfo*);
using add_execute_command_fn = void (*)(void*, void*, const MethodInfo*);
using set_is_command_playing_fn = void (*)(void*, bool, const MethodInfo*);
using sequence_parameter_fn = void* (*)(void*, const MethodInfo*);
using is_end_exam_fn = bool (*)(void*, const MethodInfo*);
using is_exam_end_complete_fn = bool (*)(void*, const MethodInfo*);
using set_exam_end_complete_fn = void (*)(void*, const MethodInfo*);
using exam_save_ctor_fn = void (*)(void*, void*, bool, const MethodInfo*);
using json_to_json_fn = Il2CppString* (*)(void*, bool, const MethodInfo*);
using object_new_fn = void* (*)(void*);
using add_play_log_fn = void (*)(void*, void*, const MethodInfo*);
using il2cpp_array_length_fn = std::uintptr_t (*)(void*);
using il2cpp_array_get_byte_length_fn = std::uintptr_t (*)(void*);
using il2cpp_array_object_header_size_fn = std::uint32_t (*)();

// Current-PC generated-card surfaces.  All methods are synchronous managed
// methods; none of these declarations are used until the runtime metadata
// token, parameter count, and executable pointer have been checked.
using generated_context_parameter_fn = void* (*)(void*, const MethodInfo*);
using generated_context_playing_card_fn = void* (*)(void*, const MethodInfo*);
using generated_context_playing_effect_fn = void* (*)(void*, const MethodInfo*);
using generated_context_random_int_fn = int (*)(void*, int, int, const MethodInfo*);
using generated_parameter_random_state_fn = std::uint32_t (*)(void*, const MethodInfo*);
using generated_create_id_execute_fn = void (*)(void*, void*, const MethodInfo*);
using generated_card_guid_fn = Il2CppString* (*)(void*, const MethodInfo*);
using generated_create_guid_fn = void (*)(void*, const MethodInfo*);
using generated_add_card_single_fn = void (*)(
    void*, void*, int, int, int, void*, const MethodInfo*);
using generated_add_card_list_fn = void (*)(
    void*, void*, int, int, int, void*, const MethodInfo*);

using il2cpp_class_get_methods_fn = MethodInfo* (*)(void*, void**);
using il2cpp_method_get_name_fn = const char* (*)(MethodInfo*);

#if defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || \
    defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE)
using candidate_pointer_getter_fn = void* (*)(void*, const MethodInfo*);
using candidate_int_getter_fn = int (*)(void*, const MethodInfo*);
using candidate_bool_getter_fn = bool (*)(void*, const MethodInfo*);
using il2cpp_class_get_rank_fn = std::uint32_t (*)(void*);
#endif


#if defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || \
    defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE)
// Defined below the candidate helpers; the forward declaration keeps the
// method-usability gate in one place without depending on declaration order.
bool is_executable_pointer(void* value) noexcept;
il2cpp_class_get_rank_fn g_il2cpp_class_get_rank{};
#endif
il2cpp_array_length_fn g_il2cpp_array_length{};

create_use_hand_fn g_create_use_hand_original{};
create_use_drink_fn g_create_use_drink_original{};
create_turn_end_fn g_create_turn_end_original{};
add_execute_command_fn g_add_execute_command_original{};
set_is_command_playing_fn g_set_is_command_playing_original{};
is_end_exam_fn g_is_end_exam_original{};
is_exam_end_complete_fn g_is_exam_end_complete_original{};
set_exam_end_complete_fn g_set_exam_end_complete_original{};
generated_context_parameter_fn g_generated_context_parameter_original{};
generated_context_playing_card_fn g_generated_context_playing_card_original{};
generated_context_playing_effect_fn g_generated_context_playing_effect_original{};
generated_context_random_int_fn g_generated_context_random_int_original{};
generated_parameter_random_state_fn g_generated_parameter_random_state_original{};
generated_create_id_execute_fn g_generated_create_id_execute_original{};
generated_card_guid_fn g_generated_card_guid_original{};
generated_create_guid_fn g_generated_create_guid_original{};
generated_add_card_single_fn g_generated_add_card_single_original{};
generated_add_card_list_fn g_generated_add_card_list_original{};
add_play_log_fn g_add_play_log_original{};
il2cpp_array_get_byte_length_fn g_il2cpp_array_get_byte_length{};
il2cpp_array_object_header_size_fn g_il2cpp_array_object_header_size{};

enum HookBit : std::uint32_t {
    kFactoryHandHook = 1U << 0,
    kFactoryDrinkHook = 1U << 1,
    kFactoryTurnEndHook = 1U << 2,
    kAddExecuteHook = 1U << 3,
    kSetPlayingHook = 1U << 4,
    kIsEndExamHook = 1U << 5,
    kGetEndCompleteHook = 1U << 6,
    kSetEndCompleteHook = 1U << 7,
    kGeneratedCreateIdHook = 1U << 8,
    kGeneratedCreateGuidHook = 1U << 9,
    kGeneratedAddCardSingleHook = 1U << 10,
    kGeneratedAddCardListHook = 1U << 11,
    kGeneratedRandomIntHook = 1U << 12,
    kGeneratedCardGuidGetterHook = 1U << 13,
    kAddPlayLogHook = 1U << 14,
};

struct HookGuard {
    std::uint32_t bit{};
    bool owner{};

    explicit HookGuard(std::uint32_t value) :
        bit(value), owner((g_hook_mask & value) == 0 && !g_stopping.load()) {
        ++g_active_hooks;
        if (owner) {
            g_hook_mask |= bit;
        }
    }

    ~HookGuard() {
        if (owner) {
            g_hook_mask &= ~bit;
        }
        --g_active_hooks;
    }
};

struct ActionIdentity {
    bool known{};
    bool is_manual{};
    const char* action_type{"unknown"};
    int play_type{-1};
    int play_index{-1};
    // Copied source-card identity lets a generated effect join to the exact
    // manual action without reading a post-state diff.  The pointer is only
    // a same-thread join key and is never sent to the writer.
    void* source_card{};
    std::string source_card_id;
    std::string source_card_guid;
    int source_card_upgrade{-1};
    std::string source_drink_id;
};

struct SnapshotCopy {
    bool captured{};
    std::string json;
    std::string error;
};

#if defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || \
    defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE)
struct HandCandidateCopy {
    int slot_index{-1};
    std::string card_id;
    std::string card_guid;
    int upgrade{-1};
    bool card_nonnull{};
    bool id_known{};
    bool guid_known{};
    bool upgrade_known{};
};


struct LegalCandidateCopy {
    bool phase_known{};
    int phase{-1};
    bool command_playing_known{};
    bool command_playing{};
    bool command_stack_empty_known{};
    bool command_stack_empty{};
    bool turn_card_play_end_known{};
    bool turn_card_play_end{};
    bool end_exam_known{};
    bool end_exam{};
    bool exam_end_complete_known{};
    bool exam_end_complete{};
    bool settled{};
    bool terminal{};
    bool drink_list_known{};
    bool drink_candidates_known{};
    bool end_turn_candidate_known{};
    bool purity_before_captured{};
    bool purity_after_captured{};
    bool purity_compared{};
    bool purity_equal{};
    std::size_t purity_before_bytes{};
    std::size_t purity_after_bytes{};
    // The hand list is copied from the same ExamSequence decision root, but
    // its per-slot predicate remains blocked until the current-PC
    // ValidateUseHandCard return ABI and purity are proven.  Keeping the
    // observed identities separate from ``actions`` lets consumers align a
    // future validator result by slot without mistaking every hand card for
    // a legal action today.
    bool hand_list_known{};
    bool hand_identity_complete{};
    std::vector<HandCandidateCopy> hand_candidates;
    std::vector<std::string> blockers;
    std::vector<std::pair<int, std::string>> drink_candidates;
};

#endif

struct PendingAction {
    ActionIdentity action;
    void* command{};
    void* sequence{};
    DWORD managed_thread_id{};
    std::uint64_t action_order{};
    bool official_action_captured{};
    bool official_action_duplicate{};
    std::uint64_t official_action_order{};
    // Transaction milestones are deliberately separate:
    //   official_action_captured = the game accepted/logged the action;
    //   queue_drain_observed     = the shared command queue physically drained;
    //   removal from this deque  = a stable Main/terminal S' was finalized.
    // A phase-changing action can therefore unblock its physical executor
    // without being mislabeled as a training transition at an intermediate
    // TurnEnd/TurnStart state.
    bool queue_drain_observed{};
    bool queue_drain_evidence_emitted{};
    SnapshotCopy before;
    // Generated identity rows are copied into the action transaction before
    // its settled after-state is emitted.  Keeping the JSON text here means
    // the writer can publish one action_order transaction without retaining
    // any managed object pointer.
    std::vector<std::string> generated_card_identities;
    std::uint32_t secondary_action_count{};
#if defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || \
    defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE)
    LegalCandidateCopy legal_candidates;
#endif
};

struct PendingActionReceipt {
    ActionIdentity action;
    void* sequence{};
    std::uint64_t action_order{};
    bool official_action_captured{};
    bool official_action_duplicate{};
    std::uint64_t official_action_order{};
    bool queue_drain_observed{};
};

PendingActionReceipt pending_action_receipt(const PendingAction& pending) {
    PendingActionReceipt result;
    result.action = pending.action;
    result.sequence = pending.sequence;
    result.action_order = pending.action_order;
    result.official_action_captured = pending.official_action_captured;
    result.official_action_duplicate = pending.official_action_duplicate;
    result.official_action_order = pending.official_action_order;
    result.queue_drain_observed = pending.queue_drain_observed;
    return result;
}

struct SequenceCaptureState {
    std::deque<PendingAction> pending_actions;
    // Official replay order is zero-based and advances at the one AddPlayLog
    // commit hook for either log origin.  It is deliberately separate from
    // the main transition's global action_order, because secondary choices
    // are actions but not independent state transitions.
    std::uint64_t next_official_action_order{};
};

struct GeneratedRandomCall {
    int minimum{};
    int maximum{};
    int result{};
};

struct GeneratedCardIdentity {
    void* card{};
    int ordinal{-1};
    bool guid_creation_observed{};
    bool guid_was_preexisting{};
    bool guid_readable{};
    std::string card_id;
    int upgrade{-1};
    std::string guid;
    int destination{-1};
    int destination_order{-1};
    bool destination_order_known{};
};

// A single AddCard call is kept on the managed stack while the original
// method runs.  This is the only point at which a generated card, its
// destination, and its exact GetRandomInt placement result share a common
// call frame.  The writer receives only the copies committed after the call.
struct GeneratedAddObservation {
    void* list{};
    void* context{};
    int from_position{-1};
    int destination{-1};
    int effect_type{-1};
    bool list_capture_complete{};
    std::vector<void*> cards;
    std::vector<GeneratedRandomCall> random_calls;
};

struct GeneratedCreateContext {
    void* executor{};
    void* context{};
    void* sequence{};
    void* parameter{};
    void* playing_card{};
    std::uint64_t action_order{};
    ActionIdentity source_action;
    bool source_action_known{};
    bool effect_known{};
    std::string effect_id;
    int effect_type{-1};
    std::string target_card_id;
    int target_upgrade{-1};
    int target_destination{-1};
    bool random_state_before_known{};
    bool random_state_after_known{};
    std::uint32_t random_state_before{};
    std::uint32_t random_state_after{};
    GeneratedAddObservation* active_add{};
    std::vector<GeneratedCardIdentity> cards;
    std::vector<GeneratedAddObservation> adds;
};

// Nested effect execution is possible (an add can synchronously trigger
// another effect).  deque keeps addresses stable while a child context is
// pushed; no managed pointer leaves this thread-local stack.
thread_local std::deque<GeneratedCreateContext> g_generated_contexts;

// A generated card can defer its lazy GUID allocation until the next state
// serializer pass.  Keep the copied effect frame alive on this managed hook
// thread until CreateGuidIfNeed observes the real empty-to-nonempty write.
// This is deliberately not a post-state identity fallback: the context is
// only finalized by the CreateGuidIfNeed hook (or emitted unresolved at the
// next action boundary), and no managed getter is called by this queue.
thread_local std::deque<GeneratedCreateContext> g_pending_generated_contexts;

// These maps contain native copied metadata only.  They are accessed on the
// managed hook thread and guarded so a future replay path cannot race a live
// callback.  No map value is handed to the writer as a managed pointer.
std::mutex g_state_mutex;
std::unordered_map<void*, ActionIdentity> g_command_identities;
std::unordered_map<void*, SequenceCaptureState> g_sequence_capture_states;
std::unordered_map<void*, void*> g_parameter_sequences;
std::atomic<std::uint64_t> g_action_order{0};

ActionIdentity hand_identity(int index, bool is_manual) {
    return ActionIdentity{true, is_manual, "use-hand", 2, index};
}

ActionIdentity drink_identity(int index, bool is_manual) {
    return ActionIdentity{true, is_manual, "use-drink", 3, index};
}

ActionIdentity turn_end_identity(bool is_manual) {
    return ActionIdentity{true, is_manual, "turn-end", 12, 0};
}

std::string int_indexes_json(const std::vector<int>& indexes) {
    InvariantStream out;
    out << '[';
    for (std::size_t index = 0; index < indexes.size(); ++index) {
        if (index != 0) {
            out << ',';
        }
        out << indexes[index];
    }
    out << ']';
    return out.str();
}

std::string normalized_action_json(
    const char* action_type,
    const std::vector<int>& indexes) {
    InvariantStream out;
    out << "{\"action_type\":\""
        << json_escape(action_type == nullptr ? "unknown" : action_type)
        << "\",\"indexes\":" << int_indexes_json(indexes) << '}';
    return out.str();
}

struct CapturedActionParent {
    bool linked{};
    bool secondary{};
    void* sequence{};
    std::uint64_t official_action_order{};
    std::uint64_t main_action_order{};
    std::int32_t selection_ordinal{-1};
    std::string main_action_type;
};

// Every native chosen-action adapter normalizes immediately into this copied
// representation.  It contains no managed pointer except the sequence value
// used only as an opaque correlation ID, and every row goes through the same
// serializer/writer below.  New action kinds must extend this spine instead
// of adding another tracker, output queue, or mode-specific capture path.
struct CapturedActionCopy {
    const char* decision_kind{};
    const char* policy_surface{};
    const char* source_boundary{};
    std::string action_type;
    std::vector<int> indexes;
    CapturedActionParent parent;
};

bool copy_int32_array(void* value, std::vector<int>& output) noexcept {
    output.clear();
    if (value == nullptr ||
        g_il2cpp_array_length == nullptr ||
        g_il2cpp_array_get_byte_length == nullptr ||
        g_il2cpp_array_object_header_size == nullptr) {
        return false;
    }
    __try {
        const std::uintptr_t length = g_il2cpp_array_length(value);
        const std::uintptr_t bytes = g_il2cpp_array_get_byte_length(value);
        const std::uint32_t header = g_il2cpp_array_object_header_size();
        if (length > kMaximumEffectCardSelectIndexes ||
            bytes != length * sizeof(int) ||
            header < sizeof(void*) * 2U || header > 256U) {
            return false;
        }
        output.resize(static_cast<std::size_t>(length));
        if (bytes != 0) {
            std::memcpy(
                output.data(),
                reinterpret_cast<const std::uint8_t*>(value) + header,
                static_cast<std::size_t>(bytes));
        }
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        output.clear();
        return false;
    }
}

std::vector<int> normalized_main_indexes(const ActionIdentity& action) {
    if (action.play_type == 12) {
        return {};
    }
    if (action.play_type == 2 || action.play_type == 3) {
        return {action.play_index};
    }
    return {};
}

enum class CaptureBindStatus {
    linked,
    untracked_parameter,
    registered_parent_unavailable,
    command_mismatch,
    duplicate,
};

CaptureBindStatus bind_captured_action(
    void* parameter,
    void* command,
    bool secondary,
    CapturedActionCopy& action) noexcept {
    if (parameter == nullptr) {
        return CaptureBindStatus::untracked_parameter;
    }
    try {
        std::lock_guard lock(g_state_mutex);
        const auto sequence_found = g_parameter_sequences.find(parameter);
        if (sequence_found == g_parameter_sequences.end()) {
            return CaptureBindStatus::untracked_parameter;
        }
        const auto state_found =
            g_sequence_capture_states.find(sequence_found->second);
        if (state_found == g_sequence_capture_states.end() ||
            state_found->second.pending_actions.empty()) {
            return CaptureBindStatus::registered_parent_unavailable;
        }
        SequenceCaptureState& state = state_found->second;
        PendingAction& parent = state.pending_actions.front();
        if (!secondary) {
            if (command == nullptr || parent.command != command) {
                return CaptureBindStatus::command_mismatch;
            }
            if (parent.official_action_captured) {
                parent.official_action_duplicate = true;
                return CaptureBindStatus::duplicate;
            }
            action.action_type = parent.action.action_type;
            action.indexes = normalized_main_indexes(parent.action);
            parent.official_action_captured = true;
            parent.official_action_order = state.next_official_action_order;
        }
        action.parent.linked = true;
        action.parent.secondary = secondary;
        action.parent.sequence = parent.sequence;
        action.parent.official_action_order = state.next_official_action_order++;
        action.parent.main_action_order = parent.action_order;
        action.parent.selection_ordinal = secondary
            ? static_cast<std::int32_t>(parent.secondary_action_count++)
            : -1;
        action.parent.main_action_type = parent.action.action_type;
        return CaptureBindStatus::linked;
    } catch (...) {
        action.parent = {};
        return CaptureBindStatus::registered_parent_unavailable;
    }
}

void emit_captured_action(const CapturedActionCopy& value) noexcept {
    if (!value.parent.linked) {
        return;
    }
    try {
        InvariantStream body;
        body << "{\"record\":\"captured_action\""
            << ",\"schema\":\"" << kCapturedActionSchema << "\""
            << ",\"decision_kind\":\""
            << json_escape(value.decision_kind == nullptr
                    ? "unknown" : value.decision_kind) << "\""
            << ",\"policy_surface\":\""
            << json_escape(value.policy_surface == nullptr
                    ? "unknown" : value.policy_surface) << "\""
            << ",\"source_boundary\":\""
            << json_escape(value.source_boundary == nullptr
                    ? "unknown" : value.source_boundary) << "\""
            << ",\"sequence\":" << pointer_json(value.parent.sequence)
            << ",\"official_action_order\":"
            << value.parent.official_action_order
            << ",\"main_action_order\":"
            << value.parent.main_action_order
            << ",\"relation\":\""
            << (value.parent.secondary ? "secondary" : "main") << "\""
            << ",\"parent\":";
        if (value.parent.secondary) {
            body << "{\"main_action_order\":"
                << value.parent.main_action_order
                << ",\"main_action_type\":\""
                << json_escape(value.parent.main_action_type)
                << "\",\"selection_ordinal\":"
                << value.parent.selection_ordinal << '}';
        } else {
            body << "null";
        }
        body
            << ",\"action\":"
            << normalized_action_json(value.action_type.c_str(), value.indexes)
            << ",\"chosen_action_exact\":true"
            << ",\"candidate_pool\":null"
            << ",\"legal_actions_complete\":false"
            << ",\"state_pair_exact\":false"
            << ",\"overlaps_parent_transition\":"
            << (value.parent.secondary ? "true" : "false")
            << ",\"flat_rl_transition\":false"
            << ",\"reward\":null}";
        emit_line(body.str());
    } catch (...) {
    }
}

void capture_main_action(
    void* parameter,
    void* command) noexcept {
    CapturedActionCopy action;
    action.decision_kind = "main-action";
    action.policy_surface = "main-action-v1";
    action.source_boundary = "ExamParameterModel.AddPlayLog(ExamPlayLog)";
    const CaptureBindStatus status = bind_captured_action(
        parameter, command, false, action);
    if (status != CaptureBindStatus::linked) {
        if (status == CaptureBindStatus::duplicate) {
            emit_error("captured-main-action-duplicate");
        } else if (
            status == CaptureBindStatus::registered_parent_unavailable) {
            emit_error("captured-action-parent-unavailable");
        }
        // AddPlayLog also commits internal/non-player logs.  A command that
        // belongs to an untracked parameter or is not the active pending
        // manual command is intentionally ignored.  A parameter registered
        // to the real sequence without a pending parent remains an error.
        return;
    }
    emit_captured_action(action);
}

void capture_effect_card_select(
    void* parameter,
    std::vector<int> indexes) noexcept {
    CapturedActionCopy action;
    action.decision_kind = "effect-card-select";
    action.policy_surface = "secondary-effect-card-select-v1";
    action.source_boundary = "ExamParameterModel.AddPlayLog(ExamPlayLog)";
    action.action_type = "effect-card-select";
    action.indexes = std::move(indexes);
    const CaptureBindStatus status = bind_captured_action(
        parameter, nullptr, true, action);
    if (status == CaptureBindStatus::untracked_parameter) {
        // The game evaluates recommendations on cloned parameter models.
        // They can commit select logs but are not the real Live/Replay
        // sequence admitted by the known-manual registration gate, so they
        // are outside this recorder's official action stream.
        return;
    }
    if (status != CaptureBindStatus::linked) {
        emit_error("captured-action-parent-unavailable");
        return;
    }
    emit_captured_action(action);
}

bool copy_il2cpp_string(Il2CppString* value, std::string& output);

bool copy_card_guid_field(void* card, std::string& output) noexcept {
    output.clear();
    if (card == nullptr || g_bindings.generated_card_guid_field == nullptr) {
        return false;
    }
    Il2CppString* value{};
    if (!recorder_read_field(
            card, g_bindings.generated_card_guid_field, value)) {
        return false;
    }
    if (value == nullptr) {
        // A newly constructed card is expected to have a null lazy GUID.  A
        // missing value is therefore a known empty identity, not an invented
        // placeholder.
        return true;
    }
    return copy_il2cpp_string(value, output);
}

void copy_source_card_identity(ActionIdentity& identity, void* card) noexcept {
    identity.source_card = card;
    identity.source_card_id.clear();
    identity.source_card_guid.clear();
    identity.source_card_upgrade = -1;
    if (card == nullptr) {
        return;
    }
    try {
        Il2CppString* card_id{};
        if (g_bindings.generated_card_id != nullptr &&
            gkms::il2cpp::call_instance_0(
                g_bindings.generated_card_id, card, card_id)) {
            copy_il2cpp_string(card_id, identity.source_card_id);
        }
        // Read the backing field rather than calling get_Guid: get_Guid is
        // itself the lazy CreateGuidIfNeed boundary and must not be caused by
        // recorder-side inspection of a source action.
        copy_card_guid_field(card, identity.source_card_guid);
        if (g_bindings.generated_card_upgrade != nullptr) {
            gkms::il2cpp::call_instance_0(
                g_bindings.generated_card_upgrade,
                card,
                identity.source_card_upgrade);
        }
    } catch (...) {
        identity.source_card_id.clear();
        identity.source_card_guid.clear();
        identity.source_card_upgrade = -1;
    }
}

std::string generic_string_value(void* object, const char* method_name) noexcept {
    if (object == nullptr || method_name == nullptr) {
        return {};
    }
    try {
        void* klass = g_api.object_class(object);
        MethodInfo* method = g_api.find_method(klass, method_name, 0);
        Il2CppString* value{};
        if (method == nullptr ||
            !gkms::il2cpp::call_instance_0(method, object, value)) {
            return {};
        }
        std::string copied;
        copy_il2cpp_string(value, copied);
        return copied;
    } catch (...) {
        return {};
    }
}

bool generic_int_value(void* object, const char* method_name, int& result) noexcept {
    result = -1;
    if (object == nullptr || method_name == nullptr) {
        return false;
    }
    try {
        void* klass = g_api.object_class(object);
        MethodInfo* method = g_api.find_method(klass, method_name, 0);
        return method != nullptr &&
            gkms::il2cpp::call_instance_0(method, object, result);
    } catch (...) {
        result = -1;
        return false;
    }
}

void remember_command(void* command, const ActionIdentity& identity) noexcept {
    if (command == nullptr) {
        return;
    }
    try {
        std::lock_guard lock(g_state_mutex);
        if (g_command_identities.size() >= kMaximumCommandIdentityEntries) {
            g_command_identities.clear();
        }
        g_command_identities[command] = identity;
    } catch (...) {
    }
}

ActionIdentity forget_command(void* command) noexcept {
    if (command == nullptr) {
        return {};
    }
    try {
        std::lock_guard lock(g_state_mutex);
        const auto found = g_command_identities.find(command);
        if (found == g_command_identities.end()) {
            return {};
        }
        ActionIdentity identity = found->second;
        g_command_identities.erase(found);
        return identity;
    } catch (...) {
        return {};
    }
}

void* parameter_for_sequence(void* sequence) noexcept {
    if (sequence == nullptr || g_bindings.sequence_parameter == nullptr) {
        return nullptr;
    }
    void* parameter{};
    if (!gkms::il2cpp::call_instance_0<void*>(
        g_bindings.sequence_parameter, sequence, parameter) || parameter == nullptr) {
        return nullptr;
    }
    return parameter;
}

void* sequence_for_parameter(void* parameter) noexcept {
    if (parameter == nullptr) {
        return nullptr;
    }
    try {
        std::lock_guard lock(g_state_mutex);
        const auto found = g_parameter_sequences.find(parameter);
        return found == g_parameter_sequences.end() ? nullptr : found->second;
    } catch (...) {
        return nullptr;
    }
}

bool readable_span(const void* value, std::size_t size) noexcept {
    if (value == nullptr || size == 0 || size > kMaximumJsonBytes * 2U) {
        return false;
    }
    const auto start = reinterpret_cast<std::uintptr_t>(value);
    if (start + size < start) {
        return false;
    }
    std::uintptr_t cursor = start;
    const std::uintptr_t end = start + size;
    while (cursor < end) {
        MEMORY_BASIC_INFORMATION region{};
        if (VirtualQuery(reinterpret_cast<const void*>(cursor), &region, sizeof(region)) !=
            sizeof(region) || region.State != MEM_COMMIT ||
            (region.Protect & (PAGE_GUARD | PAGE_NOACCESS)) != 0) {
            return false;
        }
        const auto region_end = reinterpret_cast<std::uintptr_t>(region.BaseAddress) +
            region.RegionSize;
        if (region_end <= cursor) {
            return false;
        }
        cursor = region_end;
    }
    return true;
}

bool read_il2cpp_string_fields(
    Il2CppString* value,
    const wchar_t*& characters,
    std::int32_t& length) noexcept {
    if (value == nullptr) {
        return false;
    }
    __try {
        characters = value->chars;
        length = value->length;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

bool copy_il2cpp_string(Il2CppString* value, std::string& output) {
    output.clear();
    const wchar_t* characters{};
    std::int32_t length{};
    if (!read_il2cpp_string_fields(value, characters, length)) {
        return false;
    }
    if (characters == nullptr || length <= 0 ||
        static_cast<std::size_t>(length) > kMaximumJsonBytes / sizeof(wchar_t) ||
        !readable_span(characters, static_cast<std::size_t>(length) * sizeof(wchar_t))) {
        return false;
    }
    const int needed = WideCharToMultiByte(
        CP_UTF8,
        WC_ERR_INVALID_CHARS,
        characters,
        length,
        nullptr,
        0,
        nullptr,
        nullptr);
    if (needed <= 0 || static_cast<std::size_t>(needed) > kMaximumJsonBytes) {
        return false;
    }
    try {
        output.assign(static_cast<std::size_t>(needed), '\0');
        return WideCharToMultiByte(
            CP_UTF8,
            WC_ERR_INVALID_CHARS,
            characters,
            length,
            output.data(),
            needed,
            nullptr,
            nullptr) == needed;
    } catch (...) {
        output.clear();
        return false;
    }
}

void* allocate_object(void* klass) noexcept {
    if (klass == nullptr || g_bindings.object_new == nullptr) {
        return nullptr;
    }
    void* result{};
    __try {
        result = reinterpret_cast<object_new_fn>(g_bindings.object_new)(klass);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        result = nullptr;
    }
    return result;
}

bool invoke_save_ctor(void* save, void* sequence) noexcept {
    if (save == nullptr || sequence == nullptr || g_bindings.exam_save_ctor == nullptr) {
        return false;
    }
    __try {
        reinterpret_cast<exam_save_ctor_fn>(g_bindings.exam_save_ctor->method_pointer)(
            save, sequence, false, g_bindings.exam_save_ctor);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

Il2CppString* invoke_json_to_json(void* save) noexcept {
    if (save == nullptr || g_bindings.json_to_json == nullptr) {
        return nullptr;
    }
    Il2CppString* result{};
    __try {
        result = reinterpret_cast<json_to_json_fn>(g_bindings.json_to_json->method_pointer)(
            save, false, g_bindings.json_to_json);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        result = nullptr;
    }
    return result;
}

SnapshotCopy capture_snapshot(void* sequence) noexcept {
    SnapshotCopy result;
    if (sequence == nullptr) {
        result.error = "sequence-null";
        return result;
    }
    if (g_snapshot_depth != 0) {
        result.error = "snapshot-reentrancy-suppressed";
        return result;
    }
    ++g_snapshot_depth;
    try {
        void* save = allocate_object(g_bindings.save_class);
        if (save == nullptr || !invoke_save_ctor(save, sequence)) {
            result.error = "exam-save-constructor-failed";
            --g_snapshot_depth;
            return result;
        }
        Il2CppString* json = invoke_json_to_json(save);
        if (json == nullptr || !copy_il2cpp_string(json, result.json)) {
            result.error = "jsonutility-tojson-copy-failed";
            result.json.clear();
            --g_snapshot_depth;
            return result;
        }
        result.captured = true;
    } catch (...) {
        result.captured = false;
        result.json.clear();
        result.error = "native-snapshot-copy-failed";
    }
    --g_snapshot_depth;
    return result;
}

std::string snapshot_json(const SnapshotCopy& snapshot) {
    return snapshot.captured && !snapshot.json.empty() ? snapshot.json : "null";
}

struct TerminalStatus {
    bool end_exam_known{};
    bool end_exam{};
    bool completion_known{};
    bool completion{};

    bool terminal() const noexcept {
        return end_exam_known && completion_known && end_exam && completion;
    }
};

TerminalStatus terminal_status(void* sequence) noexcept {
    TerminalStatus result;
    if (sequence != nullptr && g_is_end_exam_original != nullptr) {
        __try {
            result.end_exam = g_is_end_exam_original(sequence, g_bindings.is_end_exam);
            result.end_exam_known = true;
        } __except (EXCEPTION_EXECUTE_HANDLER) {
        }
    }
    void* parameter{};
    if (sequence != nullptr && g_bindings.sequence_parameter != nullptr) {
        gkms::il2cpp::call_instance_0<void*>(
            g_bindings.sequence_parameter, sequence, parameter);
    }
    if (parameter != nullptr && g_is_exam_end_complete_original != nullptr) {
        __try {
            result.completion = g_is_exam_end_complete_original(
                parameter, g_bindings.is_exam_end_complete);
            result.completion_known = true;
        } __except (EXCEPTION_EXECUTE_HANDLER) {
        }
    }
    return result;
}

struct ActionSettlementState {
    bool phase_known{};
    int phase{-1};
    bool command_playing_known{};
    bool command_playing{};
    bool command_stack_empty_known{};
    bool command_stack_empty{};
    bool turn_card_play_end_known{};
    bool turn_card_play_end{};
    TerminalStatus terminal;

    bool physically_drained() const noexcept {
        return command_playing_known && !command_playing &&
            command_stack_empty_known && command_stack_empty;
    }

    bool stable_main_decision() const noexcept {
        return physically_drained() && phase_known && phase == kMainPhase &&
            turn_card_play_end_known && !turn_card_play_end &&
            terminal.end_exam_known && !terminal.end_exam &&
            terminal.completion_known && !terminal.completion;
    }
};

ActionSettlementState capture_action_settlement_state(
    void* sequence) noexcept {
    ActionSettlementState result;
    if (sequence == nullptr) {
        return result;
    }
    void* parameter = parameter_for_sequence(sequence);
    if (parameter != nullptr) {
        result.phase_known = gkms::il2cpp::call_instance_0(
            g_bindings.candidate_parameter_phase,
            parameter,
            result.phase);
        result.turn_card_play_end_known = gkms::il2cpp::call_instance_0(
            g_bindings.candidate_parameter_turn_card_play_end,
            parameter,
            result.turn_card_play_end);
    }
    result.command_playing_known = gkms::il2cpp::call_instance_0(
        g_bindings.candidate_sequence_command_playing,
        sequence,
        result.command_playing);
    void* stack{};
    if (gkms::il2cpp::call_instance_0(
            g_bindings.candidate_sequence_command_stack,
            sequence,
            stack) &&
        stack != nullptr) {
        result.command_stack_empty_known = gkms::il2cpp::call_instance_0(
            g_bindings.candidate_command_stack_is_empty,
            stack,
            result.command_stack_empty);
    }
    result.terminal = terminal_status(sequence);
    return result;
}

// ``il2cpp_class_get_method_from_name`` returns the first overload.  The
// generated-card path has two AddCard overloads with identical arity, so the
// list overload must be selected by its current metadata token rather than
// by name alone.  This helper uses only exported IL2CPP reflection APIs and
// does not synthesize an RVA from the Android dump.
MethodInfo* find_method_by_token(
    void* klass,
    const char* method_name,
    std::uint32_t token,
    std::uint32_t parameter_count) noexcept {
    if (klass == nullptr) {
        return nullptr;
    }
    token = source_binding_token(klass, method_name, parameter_count, token);
    if (!token) return nullptr;
    const auto get_methods = reinterpret_cast<il2cpp_class_get_methods_fn>(
        GetProcAddress(g_api.game_assembly(), "il2cpp_class_get_methods"));
    const auto get_name = reinterpret_cast<il2cpp_method_get_name_fn>(
        GetProcAddress(g_api.game_assembly(), "il2cpp_method_get_name"));
    if (get_methods == nullptr) {
        return nullptr;
    }
    void* iterator = nullptr;
    for (std::size_t count = 0; count < 4096; ++count) {
        MethodInfo* method{};
        __try {
            method = get_methods(klass, &iterator);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            return nullptr;
        }
        if (method == nullptr) {
            break;
        }
        if (g_api.method_token(method) != token ||
            g_api.method_parameter_count(method) != parameter_count) {
            continue;
        }
        if (method_name != nullptr && get_name != nullptr) {
            const char* observed{};
            __try {
                observed = get_name(method);
            } __except (EXCEPTION_EXECUTE_HANDLER) {
                observed = nullptr;
            }
            bool name_matches = false;
            if (observed != nullptr) {
                __try {
                    std::size_t index = 0;
                    for (; index < 256 && observed[index] != '\0' &&
                           method_name[index] != '\0'; ++index) {
                        if (observed[index] != method_name[index]) break;
                    }
                    name_matches = index < 256 &&
                        observed[index] == '\0' && method_name[index] == '\0';
                } __except (EXCEPTION_EXECUTE_HANDLER) {
                    name_matches = false;
                }
            }
            if (!name_matches) {
                continue;
            }
        }
        return method;
    }
    return nullptr;
}

MethodInfo* generated_list_method(
    void* list,
    const char* name,
    int argument_count) noexcept {
    if (list == nullptr || name == nullptr) {
        return nullptr;
    }
    void* klass = g_api.object_class(list);
    return klass == nullptr ? nullptr : g_api.find_method(klass, name, argument_count);
}

bool generated_list_count(void* list, int& count) noexcept {
    count = 0;
    MethodInfo* method = generated_list_method(list, "get_Count", 0);
    if (list == nullptr || method == nullptr ||
        !gkms::il2cpp::call_instance_0(method, list, count) ||
        count < 0 || count > 4096) {
        count = 0;
        return false;
    }
    return true;
}

bool generated_list_item(void* list, int index, void*& item) noexcept {
    item = nullptr;
    MethodInfo* method = generated_list_method(list, "get_Item", 1);
    return list != nullptr && index >= 0 && method != nullptr &&
        gkms::il2cpp::call_instance_1(method, list, index, item);
}

bool copy_generated_card_list(
    void* list,
    std::vector<void*>& cards) noexcept {
    cards.clear();
    int count = 0;
    if (!generated_list_count(list, count)) {
        return false;
    }
    try {
        cards.reserve(static_cast<std::size_t>(count));
        for (int index = 0; index < count; ++index) {
            void* card{};
            if (!generated_list_item(list, index, card) || card == nullptr) {
                cards.clear();
                return false;
            }
            cards.push_back(card);
        }
    } catch (...) {
        cards.clear();
        return false;
    }
    return true;
}

GeneratedCreateContext* generated_context_for_context(void* context) noexcept {
    if (context == nullptr) {
        return nullptr;
    }
    for (auto iterator = g_generated_contexts.rbegin();
         iterator != g_generated_contexts.rend(); ++iterator) {
        if (iterator->context == context) {
            return &*iterator;
        }
    }
    return nullptr;
}

GeneratedCreateContext* generated_context_for_card_exact(void* card) noexcept {
    if (card == nullptr) {
        return nullptr;
    }
    for (auto iterator = g_generated_contexts.rbegin();
         iterator != g_generated_contexts.rend(); ++iterator) {
        for (const GeneratedCardIdentity& identity : iterator->cards) {
            if (identity.card == card) {
                return &*iterator;
            }
        }
        if (iterator->active_add != nullptr) {
            for (void* candidate : iterator->active_add->cards) {
                if (candidate == card) {
                    return &*iterator;
                }
            }
        }
    }
    return nullptr;
}

GeneratedCreateContext* generated_context_for_card(void* card) noexcept {
    if (GeneratedCreateContext* context =
            generated_context_for_card_exact(card)) {
        return context;
    }
    // A new ExamCardData can request its GUID before the enclosing AddCard
    // receives the list.  The top CardCreateId frame is the only safe owner
    // for such an event; it is reconciled by pointer when AddCard begins.
    return g_generated_contexts.empty() ? nullptr : &g_generated_contexts.back();
}

GeneratedCreateContext* generated_pending_context_for_card(void* card) noexcept {
    if (card == nullptr) {
        return nullptr;
    }
    for (auto iterator = g_pending_generated_contexts.rbegin();
         iterator != g_pending_generated_contexts.rend(); ++iterator) {
        for (const GeneratedCardIdentity& identity : iterator->cards) {
            if (identity.card == card) {
                return &*iterator;
            }
        }
        for (const GeneratedAddObservation& add : iterator->adds) {
            for (void* candidate : add.cards) {
                if (candidate == card) {
                    return &*iterator;
                }
            }
        }
    }
    return nullptr;
}

GeneratedCardIdentity& generated_card_for(
    GeneratedCreateContext& context,
    void* card,
    int ordinal = -1) {
    for (GeneratedCardIdentity& identity : context.cards) {
        if (identity.card == card) {
            if (identity.ordinal < 0 && ordinal >= 0) {
                identity.ordinal = ordinal;
            }
            return identity;
        }
    }
    context.cards.push_back({});
    GeneratedCardIdentity& identity = context.cards.back();
    identity.card = card;
    identity.ordinal = ordinal;
    return identity;
}

const char* generated_destination_name(int value) noexcept {
    switch (value) {
    case 1: return "hand";
    case 2: return "deck_first";
    case 3: return "deck_last";
    case 4: return "deck_random";
    case 5: return "grave";
    case 6: return "lost";
    case 7: return "hold";
    default: return "unknown";
    }
}

std::optional<PendingAction> generated_pending_action(
    void* sequence,
    void* playing_card) noexcept {
    if (sequence == nullptr) {
        return std::nullopt;
    }
    try {
        std::lock_guard lock(g_state_mutex);
        const auto found = g_sequence_capture_states.find(sequence);
        if (found == g_sequence_capture_states.end() ||
            found->second.pending_actions.empty()) {
            return std::nullopt;
        }
        const auto& queue = found->second.pending_actions;
        if (playing_card != nullptr) {
            for (auto iterator = queue.rbegin();
                 iterator != queue.rend(); ++iterator) {
                if (iterator->action.source_card == playing_card) {
                    return *iterator;
                }
            }
        }
        // One pending row is unambiguous.  More than one without a source
        // card match is deliberately rejected; action order must never be
        // guessed from queue position after a continuity break.
        if (queue.size() == 1) {
            return queue.front();
        }
    } catch (...) {
    }
    return std::nullopt;
}

void attach_generated_identity(
    void* sequence,
    std::uint64_t action_order,
    const std::string& identity_json) noexcept {
    if (sequence == nullptr || action_order == 0 || identity_json.empty()) {
        return;
    }
    try {
        std::lock_guard lock(g_state_mutex);
        const auto found = g_sequence_capture_states.find(sequence);
        if (found == g_sequence_capture_states.end()) {
            return;
        }
        for (PendingAction& pending : found->second.pending_actions) {
            if (pending.action_order != action_order) {
                continue;
            }
            if (std::find(
                    pending.generated_card_identities.begin(),
                    pending.generated_card_identities.end(),
                    identity_json) != pending.generated_card_identities.end()) {
                return;
            }
            if (pending.generated_card_identities.size() < 64) {
                pending.generated_card_identities.push_back(identity_json);
            }
            return;
        }
    } catch (...) {
    }
}

bool generated_effect_info(
    void* context,
    std::string& effect_id,
    int& effect_type) noexcept {
    effect_id.clear();
    effect_type = -1;
    void* effect{};
    if (context == nullptr || g_bindings.generated_context_playing_effect == nullptr ||
        !gkms::il2cpp::call_instance_0(
            g_bindings.generated_context_playing_effect, context, effect) ||
        effect == nullptr) {
        return false;
    }
    effect_id = generic_string_value(effect, "get_Id");
    generic_int_value(effect, "get_EffectType", effect_type);
    return !effect_id.empty() && effect_type >= 0;
}

void generated_refresh_card_identity(
    GeneratedCardIdentity& identity) noexcept {
    identity.card_id.clear();
    identity.guid.clear();
    identity.upgrade = -1;
    identity.guid_readable = false;
    if (identity.card == nullptr) {
        return;
    }
    try {
        Il2CppString* card_id{};
        if (g_bindings.generated_card_id != nullptr &&
            gkms::il2cpp::call_instance_0(
                g_bindings.generated_card_id, identity.card, card_id)) {
            copy_il2cpp_string(card_id, identity.card_id);
        }
        if (g_bindings.generated_card_upgrade != nullptr) {
            gkms::il2cpp::call_instance_0(
                g_bindings.generated_card_upgrade,
                identity.card,
                identity.upgrade);
        }
        identity.guid_readable = copy_card_guid_field(identity.card, identity.guid);
    } catch (...) {
        identity.card_id.clear();
        identity.guid.clear();
        identity.upgrade = -1;
        identity.guid_readable = false;
    }
}

#if defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || \
    defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE)

void add_candidate_blocker(LegalCandidateCopy& result, const char* reason) noexcept {
    if (reason == nullptr || result.blockers.size() >= kMaximumCandidateBlockers) {
        return;
    }
    try {
        if (std::find(result.blockers.begin(), result.blockers.end(), reason) ==
            result.blockers.end()) {
            result.blockers.emplace_back(reason);
        }
    } catch (...) {
        // Candidate output is deliberately best-effort shadow evidence.  A
        // failed diagnostic allocation must never turn into a legal action.
    }
}

void record_candidate_purity(
    LegalCandidateCopy& result,
    const SnapshotCopy& before,
    const SnapshotCopy& after) noexcept {
    // Compare the exact copied UTF-8 bytes produced by the same managed
    // serializer.  This is a probe-only mutation check: it is not a
    // reconstruction or normalization of state, and it never promotes the
    // provisional candidates into authoritative legal_actions.
    result.purity_before_captured = before.captured;
    result.purity_after_captured = after.captured;
    result.purity_before_bytes = before.captured ? before.json.size() : 0;
    result.purity_after_bytes = after.captured ? after.json.size() : 0;
    if (!before.captured) {
        add_candidate_blocker(
            result,
            "legal-candidate-purity-before-snapshot-unavailable");
        return;
    }
    if (!after.captured) {
        add_candidate_blocker(
            result,
            "legal-candidate-purity-after-snapshot-unavailable");
        return;
    }
    result.purity_compared = true;
    result.purity_equal = before.json == after.json;
    if (!result.purity_equal) {
        add_candidate_blocker(
            result,
            "legal-candidate-enumeration-mutated-state");
    }
}

bool candidate_method_usable(MethodInfo* method) noexcept {
    return method != nullptr && method->method_pointer != nullptr &&
        is_executable_pointer(method->method_pointer);
}

MethodInfo* candidate_list_method(
    void* list,
    const char* name,
    int argument_count,
    MethodInfo* fallback) noexcept {
    if (list != nullptr) {
        void* list_class = g_api.object_class(list);
        if (list_class != nullptr) {
            MethodInfo* method = g_api.find_method(
                list_class, name, argument_count);
            if (candidate_method_usable(method)) {
                return method;
            }
        }
    }
    return candidate_method_usable(fallback) ? fallback : nullptr;
}

bool candidate_list_count(void* list, int& count) noexcept {
    count = 0;
    void* klass = list == nullptr ? nullptr : g_api.object_class(list);
    if (klass != nullptr && g_il2cpp_class_get_rank != nullptr &&
        g_il2cpp_array_length != nullptr) {
        std::uint32_t rank{};
        __try {
            rank = g_il2cpp_class_get_rank(klass);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            rank = 0;
        }
        if (rank > 0) {
            if (rank != 1) {
                return false;
            }
            std::uintptr_t length{};
            __try {
                length = g_il2cpp_array_length(list);
            } __except (EXCEPTION_EXECUTE_HANDLER) {
                return false;
            }
            if (length > kMaximumCandidateListItems) {
                return false;
            }
            count = static_cast<int>(length);
            return true;
        }
    }
    MethodInfo* method = candidate_list_method(
        list, "get_Count", 0, g_bindings.candidate_list_count);
    if (list == nullptr || method == nullptr ||
        !gkms::il2cpp::call_instance_0(method, list, count) ||
        count < 0 || static_cast<std::size_t>(count) > kMaximumCandidateListItems) {
        count = 0;
        return false;
    }
    return true;
}

bool candidate_list_item(void* list, int index, void*& item) noexcept {
    item = nullptr;
    if (list == nullptr || index < 0) {
        return false;
    }
    MethodInfo* method = candidate_list_method(
        list, "get_Item", 1, g_bindings.candidate_list_item);
    return method != nullptr &&
        gkms::il2cpp::call_instance_1(method, list, index, item);
}

std::string candidate_drink_id(void* drink) noexcept {
    if (drink == nullptr) {
        return {};
    }
    try {
        MethodInfo* method = g_bindings.candidate_drink_id;
        void* drink_class = g_api.object_class(drink);
        if (drink_class != nullptr) {
            MethodInfo* concrete = g_api.find_method(drink_class, "get_Id", 0);
            if (candidate_method_usable(concrete)) {
                method = concrete;
            }
        }
        if (!candidate_method_usable(method)) {
            return {};
        }
        Il2CppString* id{};
        if (!gkms::il2cpp::call_instance_0(method, drink, id)) {
            return {};
        }
        // ``utf8`` is intentionally not used here: this is a noexcept
        // candidate path and a malformed/oversized managed string must become
        // an unknown identity, never an exception escaping the hook.
        std::string copied;
        return copy_il2cpp_string(id, copied) ? copied : std::string{};
    } catch (...) {
        return {};
    }
}

bool copy_hand_candidate(
    void* card,
    int slot_index,
    HandCandidateCopy& result) noexcept {
    result = {};
    result.slot_index = slot_index;
    result.card_nonnull = card != nullptr;
    if (card == nullptr) {
        return false;
    }
    try {
        Il2CppString* id{};
        if (g_bindings.generated_card_id == nullptr ||
            !gkms::il2cpp::call_instance_0(
                g_bindings.generated_card_id, card, id) ||
            !copy_il2cpp_string(id, result.card_id)) {
            result.card_id.clear();
            result.id_known = false;
        } else {
            result.id_known = true;
        }

        // Reading the backing field is deliberately used instead of calling
        // ExamCardData.get_Guid.  The latter is a lazy CreateGuidIfNeed
        // boundary and a recorder-side read must not allocate identity or
        // mutate the Exam state during a legal-candidate probe.
        result.guid_known = copy_card_guid_field(card, result.card_guid);

        if (g_bindings.generated_card_upgrade != nullptr) {
            result.upgrade_known = gkms::il2cpp::call_instance_0(
                g_bindings.generated_card_upgrade, card, result.upgrade);
        }
        // A readable null backing field is useful evidence that the card has
        // not materialized a GUID yet, but it is not a complete card
        // identity.  Keep the distinction explicit for fail-closed output.
        return result.id_known && result.guid_known && !result.card_guid.empty() &&
            result.upgrade_known;
    } catch (...) {
        result.card_id.clear();
        result.card_guid.clear();
        result.upgrade = -1;
        result.id_known = false;
        result.guid_known = false;
        result.upgrade_known = false;
        return false;
    }
}


std::string optional_bool_json(bool known, bool value) {
    return known ? (value ? "true" : "false") : "null";
}

void append_candidate_action(
    InvariantStream& out,
    const char* kind,
    int slot_index,
    const std::string& identity,
    bool turn_end = false) {
    out << "{\"kind\":\"" << kind << "\""
        << ",\"action_type\":\"" << kind << "\""
        << ",\"slot_index\":" << slot_index;
    if (turn_end) {
        out << ",\"action_id\":\"END_TURN\"";
    } else {
        out << ",\"drink_id\":"
            << (identity.empty() ? "null" :
                std::string("\"") + json_escape(identity) + "\"");
    }
    out << ",\"identity_complete\":"
        << ((turn_end || !identity.empty()) ? "true" : "false")
        << ",\"source\":\"runtime-getter-only\""
        << ",\"integrity\":{\"slot_index_observed\":true"
        << ",\"identity_observed\":"
        << ((turn_end || !identity.empty()) ? "true" : "false")
        << "}}";
}

std::string drink_candidates_json(const LegalCandidateCopy& value) {
    if (!value.drink_candidates_known) {
        return "null";
    }
    InvariantStream out;
    out << "[";
    for (std::size_t index = 0; index < value.drink_candidates.size(); ++index) {
        if (index != 0) {
            out << ',';
        }
        append_candidate_action(
            out,
            "use-drink",
            value.drink_candidates[index].first,
            value.drink_candidates[index].second);
    }
    out << "]";
    return out.str();
}

std::string hand_observed_candidates_json(const LegalCandidateCopy& value) {
    if (!value.hand_list_known) {
        return "null";
    }
    InvariantStream out;
    out << "[";
    for (std::size_t index = 0; index < value.hand_candidates.size(); ++index) {
        if (index != 0) {
            out << ',';
        }
        const HandCandidateCopy& card = value.hand_candidates[index];
        out << "{\"kind\":\"use-hand\""
            << ",\"action_type\":\"use-hand\""
            << ",\"slot_index\":" << card.slot_index
            // ``card_index`` is an explicit alias for callers that use the
            // action vocabulary rather than the hand-slot vocabulary.  Both
            // values are copied from the same native list ordinal.
            << ",\"card_index\":" << card.slot_index
            << ",\"card_id\":"
            << (card.id_known && !card.card_id.empty()
                ? std::string("\"") + json_escape(card.card_id) + "\""
                : "null")
            << ",\"card_guid\":"
            << (card.guid_known && !card.card_guid.empty()
                ? std::string("\"") + json_escape(card.card_guid) + "\""
                : "null")
            // Keep the short alias used by existing card identity adapters;
            // it is the same copied backing-field value, never a generated
            // placeholder.
            << ",\"guid\":"
            << (card.guid_known && !card.card_guid.empty()
                ? std::string("\"") + json_escape(card.card_guid) + "\""
                : "null")
            << ",\"upgrade\":"
            << (card.upgrade_known ? std::to_string(card.upgrade) : "null")
            // The slot is observed, but legality is intentionally unknown
            // until the native ValidateUseHandCard ABI/purity probe passes.
            << ",\"legal\":null"
            << ",\"source\":\"ExamSequence.get_HandList\""
            << ",\"identity_complete\":"
            << (card.id_known && card.guid_known && !card.card_guid.empty() &&
                card.upgrade_known
                ? "true" : "false")
            << ",\"integrity\":{\"slot_index_observed\":true"
            << ",\"card_id_observed\":"
            << (card.id_known ? "true" : "false")
            << ",\"card_guid_observed\":"
            << (card.guid_known && !card.card_guid.empty() ? "true" : "false")
            << ",\"upgrade_observed\":"
            << (card.upgrade_known ? "true" : "false")
            << ",\"legality_predicate\":\"unverified\"}}";
    }
    out << "]";
    return out.str();
}

std::string all_candidates_json(const LegalCandidateCopy& value) {
    if (value.terminal) {
        return "[]";
    }
    if (!value.settled || !value.drink_candidates_known ||
        !value.end_turn_candidate_known) {
        return "null";
    }
    InvariantStream out;
    out << "[";
    bool needs_comma = false;
    for (const auto& drink : value.drink_candidates) {
        if (needs_comma) {
            out << ',';
        }
        append_candidate_action(out, "use-drink", drink.first, drink.second);
        needs_comma = true;
    }
    if (needs_comma) {
        out << ',';
    }
    append_candidate_action(out, "turn-end", 0, {}, true);
    out << "]";
    return out.str();
}

std::string legal_candidate_json(const LegalCandidateCopy& value) {
    InvariantStream out;
    out << "{\"schema\":\"gkms.runtime-exam-legal-action-candidates.shadow.v1\""
        << ",\"decision_kind\":\"main\""
        << ",\"basis\":\"same-exam-sequence-getter-only\""
        << ",\"source\":\"same-ExamSequence-runtime\""
        << ",\"shadow\":true"
        << ",\"exact\":false"
        << ",\"complete\":false"
        << ",\"legal_actions_complete\":false"
        << ",\"legal_actions\":null"
        << ",\"authoritative_legal_actions\":null"
        << ",\"phase\":" << (value.phase_known ?
            std::to_string(value.phase) : "null")
        << ",\"is_command_playing\":"
        << optional_bool_json(value.command_playing_known, value.command_playing)
        << ",\"command_stack_empty\":"
        << optional_bool_json(value.command_stack_empty_known, value.command_stack_empty)
        << ",\"is_turn_card_play_end\":"
        << optional_bool_json(value.turn_card_play_end_known, value.turn_card_play_end)
        << ",\"is_end_exam\":"
        << optional_bool_json(value.end_exam_known, value.end_exam)
        << ",\"is_exam_end_complete\":"
        << optional_bool_json(value.exam_end_complete_known, value.exam_end_complete)
        << ",\"hand_list_known\":"
        << (value.hand_list_known ? "true" : "false")
        << ",\"hand_identity_complete\":"
        << (value.hand_identity_complete ? "true" : "false")
        << ",\"settled\":" << (value.settled ? "true" : "false")
        << ",\"terminal\":" << (value.terminal ? "true" : "false")
        << ",\"enumeration_purity\":{\"before_captured\":"
        << (value.purity_before_captured ? "true" : "false")
        << ",\"after_captured\":"
        << (value.purity_after_captured ? "true" : "false")
        << ",\"compared\":"
        << (value.purity_compared ? "true" : "false")
        << ",\"equal\":"
        << (value.purity_compared ?
            (value.purity_equal ? "true" : "false") : "null")
        << ",\"before_bytes\":" << value.purity_before_bytes
        << ",\"after_bytes\":" << value.purity_after_bytes << "}"
        << ",\"hand\":{\"status\":\"blocked\",\"complete\":false"
        << ",\"exact\":false,\"actions\":null"
        << ",\"observed_candidates\":"
        << hand_observed_candidates_json(value)
        << ",\"source\":\"ExamSequence.get_HandList\""
        << ",\"identity_complete\":"
        << (value.hand_identity_complete ? "true" : "false")
        << ",\"blockers\":[\"current-pc-use-hand-validator-purity-and-abi-unverified\"]}"
        << ",\"drink\":{\"status\":\""
        << (value.drink_candidates_known ? "candidate" : "blocked")
        << "\",\"complete\":false,\"exact\":false"
        << ",\"actions\":" << drink_candidates_json(value)
        << ",\"source\":\"ExamParameterModel.get_DrinkList+ProduceDrinkData.get_ProduceEffectList\""
        << ",\"predicate\":\"phase==Main(6) && !is_command_playing && "
           "drink.ProduceEffectList.Count>0\"}"
        << ",\"end_turn\":{\"status\":\""
        << (value.end_turn_candidate_known ? "candidate" : "blocked")
        << "\",\"complete\":false,\"exact\":false"
        << ",\"source\":\"ExamSequence.get_Parameter+settled-main-guards\""
        << ",\"actions\":";
    if (value.terminal) {
        out << "[]";
    } else if (value.end_turn_candidate_known) {
        out << "[{\"kind\":\"turn-end\",\"action_type\":\"turn-end\","
               "\"slot_index\":0,\"action_id\":\"END_TURN\","
               "\"identity_complete\":true,\"source\":\"runtime-getter-only\","
               "\"integrity\":{\"slot_index_observed\":true,"
               "\"predicate_observed\":true}}]";
    } else {
        out << "null";
    }
    out << ",\"predicate\":\"phase==Main(6) && !is_command_playing"
           " plus settled/terminal guards\"}";
    out << ",\"actions\":" << all_candidates_json(value)
        << ",\"integrity\":{\"candidate_set_complete\":false"
        << ",\"hand_legality\":\"unverified\""
        << ",\"drink_predicate\":\"runtime-getter-only\""
        << ",\"end_turn_predicate\":\"runtime-getter-only\"}"
        << ",\"blockers\":[";
    for (std::size_t index = 0; index < value.blockers.size(); ++index) {
        if (index != 0) {
            out << ',';
        }
        out << '"' << json_escape(value.blockers[index]) << '"';
    }
    out << ']';
    out << "}";
    return out.str();
}

LegalCandidateCopy enumerate_legal_candidates(void* sequence) noexcept {
    LegalCandidateCopy result;
    if (sequence == nullptr) {
        add_candidate_blocker(result, "sequence-null");
        return result;
    }

    void* parameter{};
    if (g_bindings.sequence_parameter == nullptr ||
        !gkms::il2cpp::call_instance_0(
            g_bindings.sequence_parameter, sequence, parameter) ||
        parameter == nullptr) {
        add_candidate_blocker(result, "parameter-getter-failed");
        return result;
    }

    if (g_bindings.candidate_parameter_phase == nullptr ||
        !gkms::il2cpp::call_instance_0(
            g_bindings.candidate_parameter_phase, parameter, result.phase)) {
        add_candidate_blocker(result, "phase-getter-failed");
    } else {
        result.phase_known = true;
    }
    MethodInfo* playing_getter = g_bindings.candidate_sequence_command_playing;
    if (candidate_method_usable(playing_getter) &&
        gkms::il2cpp::call_instance_0(
            playing_getter, sequence, result.command_playing)) {
        result.command_playing_known = true;
    } else {
        add_candidate_blocker(result, "command-playing-getter-failed");
    }

    void* stack{};
    if (g_bindings.candidate_sequence_command_stack == nullptr ||
        !gkms::il2cpp::call_instance_0(
            g_bindings.candidate_sequence_command_stack, sequence, stack) ||
        stack == nullptr) {
        add_candidate_blocker(result, "command-stack-getter-failed");
    } else if (g_bindings.candidate_command_stack_is_empty == nullptr ||
        !gkms::il2cpp::call_instance_0(
            g_bindings.candidate_command_stack_is_empty,
            stack,
            result.command_stack_empty)) {
        add_candidate_blocker(result, "command-stack-empty-getter-failed");
    } else {
        result.command_stack_empty_known = true;
    }
    if (g_bindings.candidate_parameter_turn_card_play_end == nullptr ||
        !gkms::il2cpp::call_instance_0(
            g_bindings.candidate_parameter_turn_card_play_end,
            parameter,
            result.turn_card_play_end)) {
        add_candidate_blocker(result, "turn-card-play-end-getter-failed");
    } else {
        result.turn_card_play_end_known = true;
    }

    const TerminalStatus terminal = terminal_status(sequence);
    result.end_exam_known = terminal.end_exam_known;
    result.end_exam = terminal.end_exam;
    result.exam_end_complete_known = terminal.completion_known;
    result.exam_end_complete = terminal.completion;
    result.terminal = terminal.terminal();
    if (!result.end_exam_known) {
        add_candidate_blocker(result, "is-end-exam-getter-failed");
    }
    if (!result.exam_end_complete_known) {
        add_candidate_blocker(result, "exam-end-complete-getter-failed");
    }

    const bool stable_main = result.phase_known && result.phase == 6 &&
        result.command_playing_known && !result.command_playing &&
        result.command_stack_empty_known && result.command_stack_empty &&
        result.turn_card_play_end_known && !result.turn_card_play_end &&
        result.end_exam_known && !result.end_exam;
    result.settled = stable_main && !result.terminal;
    if (result.terminal) {
        add_candidate_blocker(result, "terminal-authoritative-empty-candidate-set");
        result.end_turn_candidate_known = true;
        result.drink_candidates_known = true;
        return result;
    }
    if (!result.settled) {
        add_candidate_blocker(result, "requires-settled-main-decision");
        return result;
    }

    // Copy the ordered hand from the same ExamSequence decision root.  This
    // is identity evidence only: until ValidateUseHandCard's Windows return
    // ABI and no-mutation behavior are proven, these rows stay under
    // ``observed_candidates`` and never become legal actions.
    void* hand_list{};
    if (g_bindings.candidate_sequence_hand_list == nullptr ||
        !gkms::il2cpp::call_instance_0(
            g_bindings.candidate_sequence_hand_list, sequence, hand_list) ||
        hand_list == nullptr) {
        add_candidate_blocker(result, "hand-list-getter-failed");
    } else {
        int hand_count{};
        if (!candidate_list_count(hand_list, hand_count)) {
            add_candidate_blocker(result, "hand-list-count-getter-failed");
        } else {
            bool hand_structure_complete = true;
            bool hand_identity_complete = true;
            try {
                result.hand_candidates.reserve(static_cast<std::size_t>(hand_count));
                for (int index = 0; index < hand_count; ++index) {
                    void* card{};
                    HandCandidateCopy copied;
                    if (!candidate_list_item(hand_list, index, card) || card == nullptr) {
                        hand_structure_complete = false;
                        hand_identity_complete = false;
                        add_candidate_blocker(result, "hand-slot-read-failed");
                        copied.slot_index = index;
                        copied.card_nonnull = false;
                    } else if (!copy_hand_candidate(card, index, copied)) {
                        hand_identity_complete = false;
                        add_candidate_blocker(result, "hand-card-identity-read-failed");
                    }
                    result.hand_candidates.push_back(std::move(copied));
                }
                result.hand_list_known = hand_structure_complete;
                result.hand_identity_complete = hand_structure_complete &&
                    hand_identity_complete;
            } catch (...) {
                result.hand_candidates.clear();
                result.hand_list_known = false;
                result.hand_identity_complete = false;
                add_candidate_blocker(result, "hand-candidate-copy-failed");
            }
        }
    }

    result.end_turn_candidate_known = true;
    void* drink_list{};
    if (g_bindings.candidate_parameter_drink_list == nullptr ||
        !gkms::il2cpp::call_instance_0(
            g_bindings.candidate_parameter_drink_list, parameter, drink_list) ||
        drink_list == nullptr) {
        add_candidate_blocker(result, "drink-list-getter-failed");
        return result;
    }
    int drink_count{};
    if (!candidate_list_count(drink_list, drink_count)) {
        add_candidate_blocker(result, "drink-list-count-getter-failed");
        return result;
    }
    try {
        std::vector<std::pair<int, std::string>> copied;
        copied.reserve(static_cast<std::size_t>(drink_count));
        for (int index = 0; index < drink_count; ++index) {
            void* drink{};
            if (!candidate_list_item(drink_list, index, drink) || drink == nullptr) {
                add_candidate_blocker(result, "drink-slot-read-failed");
                return result;
            }
            void* effects{};
            if (g_bindings.candidate_drink_effect_list == nullptr ||
                !gkms::il2cpp::call_instance_0(
                    g_bindings.candidate_drink_effect_list, drink, effects)) {
                add_candidate_blocker(result, "drink-effect-list-getter-failed");
                return result;
            }
            int effect_count{};
            if (effects != nullptr && !candidate_list_count(effects, effect_count)) {
                add_candidate_blocker(result, "drink-effect-list-count-getter-failed");
                return result;
            }
            if (effects != nullptr && effect_count > 0) {
                std::string drink_id = candidate_drink_id(drink);
                if (drink_id.empty()) {
                    add_candidate_blocker(result, "drink-identity-getter-failed");
                }
                copied.emplace_back(index, std::move(drink_id));
            }
        }
        result.drink_candidates = std::move(copied);
        result.drink_list_known = true;
        result.drink_candidates_known = true;
    } catch (...) {
        result.drink_candidates.clear();
        add_candidate_blocker(result, "drink-candidate-copy-failed");
    }
    return result;
}

void emit_candidate_boundary(
    void* sequence,
    std::uint64_t action_order,
    const LegalCandidateCopy& candidates) noexcept {
    try {
        InvariantStream body;
        body << "{\"record\":\"legal_action_candidates\""
            << ",\"boundary\":\"ExamSequence.AddExecuteCommand(next-manual-before)\""
            << ",\"sequence\":" << pointer_json(sequence)
            << ",\"action_order\":" << action_order
            << ",\"candidates\":" << legal_candidate_json(candidates)
            << "}";
        emit_line(body.str());
    } catch (...) {
    }
}

#endif

std::string action_json(const ActionIdentity& action) {
    InvariantStream out;
    out << "{\"known\":" << (action.known ? "true" : "false")
        << ",\"action_type\":\"" << json_escape(action.action_type) << "\""
        << ",\"play_type\":" << action.play_type
        << ",\"play_index\":" << action.play_index
        // Keep the managed factory bit for both Live and Replay.  The
        // candidate never relabels replay actions or infers it from UI.
        << ",\"isManual\":" << (action.is_manual ? "true" : "false")
        << ",\"manual_play_index_semantics\":\""
        << (action.play_type == 2 ? "hand_slot" :
            (action.play_type == 3 ? "drink_slot" :
                (action.play_type == 12 ? "turn_end_zero" : "unknown")))
        << "\"";
    if (action.play_type == 2 && !action.source_card_id.empty()) {
        out << ",\"source_card\":{\"id\":\""
            << json_escape(action.source_card_id)
            << "\",\"guid\":"
            << (action.source_card_guid.empty()
                ? "null"
                : std::string("\"") + json_escape(action.source_card_guid) + "\"")
            << ",\"upgrade\":" << action.source_card_upgrade << "}";
    } else if (action.play_type == 3 && !action.source_drink_id.empty()) {
        out << ",\"source_drink_id\":\""
            << json_escape(action.source_drink_id) << "\"";
    } else {
        out << ",\"source_card\":null";
    }
    out << "}";
    return out.str();
}

void emit_factory(
    const char* factory,
    const ActionIdentity& action,
    void* command) noexcept {
    try {
        InvariantStream body;
        body << "{\"record\":\"command_factory\""
            << ",\"factory\":\"" << json_escape(factory) << "\""
            << ",\"action\":" << action_json(action)
            << ",\"command\":" << pointer_json(command)
            << ",\"source\":\"live-or-replay-runtime\"}";
        emit_line(body.str());
    } catch (...) {
    }
}

std::string generated_action_source_json(const ActionIdentity& action) {
    InvariantStream out;
    out << "{\"action_order_source\":\"ExamSequence.AddExecuteCommand\""
        << ",\"action\":" << action_json(action)
        << "}";
    return out.str();
}

bool generated_card_matches(
    const GeneratedCardIdentity& identity,
    const GeneratedCreateContext& context) noexcept {
    return identity.card != nullptr && identity.guid_readable &&
        identity.guid_creation_observed && !identity.guid_was_preexisting &&
        !identity.guid.empty() && !identity.card_id.empty() &&
        identity.card_id == context.target_card_id &&
        identity.upgrade == context.target_upgrade &&
        identity.destination_order_known;
}

void generated_assign_placement(GeneratedCreateContext& context) noexcept {
    // AddCard's list overload passes the cards in constructor/source order.
    // The native DeckRandom implementation samples one GetRandomInt result
    // per card before applying stable insertion order.  Pairing by object
    // pointer preserves that source ordinal and does not infer an identity
    // from a later serialized deck diff.
    for (const GeneratedAddObservation& add : context.adds) {
        if (!add.list_capture_complete || add.destination != 4 ||
            add.random_calls.size() != add.cards.size()) {
            continue;
        }
        for (std::size_t index = 0; index < add.cards.size(); ++index) {
            GeneratedCardIdentity& identity = generated_card_for(
                context, add.cards[index], static_cast<int>(index));
            identity.destination = add.destination;
            identity.destination_order = add.random_calls[index].result;
            identity.destination_order_known = true;
        }
    }
}

std::string generated_card_identity_json(
    const GeneratedCardIdentity& identity,
    const GeneratedCreateContext& context) {
    InvariantStream out;
    out << "{\"ordinal\":" << identity.ordinal
        << ",\"card_id\":"
        << (identity.card_id.empty()
            ? "null"
            : std::string("\"") + json_escape(identity.card_id) + "\"")
        << ",\"upgrade\":" << identity.upgrade
        << ",\"guid\":"
        << (identity.guid.empty()
            ? "null"
            : std::string("\"") + json_escape(identity.guid) + "\"")
        << ",\"destination\":\""
        << json_escape(generated_destination_name(identity.destination)) << "\""
        << ",\"destination_type\":" << identity.destination
        << ",\"destination_order\":"
        << (identity.destination_order_known
            ? std::to_string(identity.destination_order) : "null")
        // ``insertion_index`` and ``order`` are aliases consumed by the
        // Python Plan2 adapter; both are copied from the same native random
        // result and never reconstructed from a post-state list.
        << ",\"insertion_index\":"
        << (identity.destination_order_known
            ? std::to_string(identity.destination_order) : "null")
        << ",\"order\":"
        << (identity.destination_order_known
            ? std::to_string(identity.destination_order) : "null")
        << ",\"guid_creation\":{\"hook\":\"ExamCardData.CreateGuidIfNeed\""
        << ",\"observed\":"
        << (identity.guid_creation_observed ? "true" : "false")
        << ",\"preexisting\":"
        << (identity.guid_was_preexisting ? "true" : "false")
        << ",\"readable\":"
        << (identity.guid_readable ? "true" : "false") << "}"
        << ",\"target_match\":"
        << ((identity.card_id == context.target_card_id &&
             identity.upgrade == context.target_upgrade) ? "true" : "false")
        << "}";
    return out.str();
}

std::string generated_create_json(const GeneratedCreateContext& context) {
    std::vector<std::string> blockers;
    auto add_blocker = [&blockers](const char* value) {
        if (value == nullptr) return;
        if (std::find(blockers.begin(), blockers.end(), value) == blockers.end()) {
            blockers.emplace_back(value);
        }
    };
    if (!context.source_action_known || context.action_order == 0) {
        add_blocker("source-action-order-unavailable");
    }
    if (context.source_action.play_type != 2 ||
        context.source_action.source_card_guid.empty()) {
        add_blocker("source-action-card-guid-unavailable");
    }
    if (!context.effect_known || context.effect_id.empty()) {
        add_blocker("playing-effect-id-unavailable");
    }
    if (context.effect_type != 6) {
        add_blocker("executor-effect-type-mismatch");
    }
    if (context.target_card_id.empty()) {
        add_blocker("target-card-id-unavailable");
    }
    if (context.target_upgrade < 0) {
        add_blocker("target-upgrade-unavailable");
    }
    if (context.target_destination < 1 || context.target_destination > 7) {
        add_blocker("target-destination-unavailable");
    }
    if (context.cards.empty()) {
        add_blocker("generated-card-list-empty");
    }
    if (context.adds.empty()) {
        add_blocker("card-move-add-boundary-unobserved");
    }
    if (!context.random_state_before_known || !context.random_state_after_known) {
        add_blocker("random-state-boundary-unavailable");
    }
    for (const GeneratedAddObservation& add : context.adds) {
        if (!add.list_capture_complete) {
            add_blocker("card-move-list-capture-failed");
        }
        if (add.context != context.context) {
            add_blocker("card-move-context-mismatch");
        }
        if (add.effect_type != 6) {
            add_blocker("card-move-effect-type-mismatch");
        }
        if (add.destination != context.target_destination) {
            add_blocker("card-move-destination-mismatch");
        }
        if (add.destination == 4 && add.random_calls.size() != add.cards.size()) {
            add_blocker("deck-random-placement-call-count-mismatch");
        }
    }
    for (const GeneratedCardIdentity& identity : context.cards) {
        if (!generated_card_matches(identity, context)) {
            add_blocker("generated-card-guid-or-placement-unresolved");
        }
    }
    std::vector<std::string> guids;
    for (const GeneratedCardIdentity& identity : context.cards) {
        if (!identity.guid.empty()) guids.push_back(identity.guid);
    }
    std::sort(guids.begin(), guids.end());
    if (std::adjacent_find(guids.begin(), guids.end()) != guids.end()) {
        add_blocker("generated-card-guid-duplicate");
    }
    const bool exact = blockers.empty();
    std::vector<std::size_t> card_order;
    card_order.reserve(context.cards.size());
    for (std::size_t index = 0; index < context.cards.size(); ++index) {
        card_order.push_back(index);
    }
    std::sort(card_order.begin(), card_order.end(),
        [&context](std::size_t left, std::size_t right) {
            const int left_ordinal = context.cards[left].ordinal;
            const int right_ordinal = context.cards[right].ordinal;
            if (left_ordinal < 0) return right_ordinal >= 0;
            if (right_ordinal < 0) return false;
            return left_ordinal < right_ordinal;
        });

    InvariantStream out;
    // No synthetic GUID, post-state diff, or guessed destination is allowed
    // to make this row exact; every identity field below comes from a
    // synchronous managed hook observation.
    out << "{\"record\":\"generated_card_identity\""
        << ",\"identity_schema\":\"gkms.runtime-generated-card-identity.v1\""
        << ",\"identity_exact\":" << (exact ? "true" : "false")
        << ",\"exact_generated_identity\":" << (exact ? "true" : "false")
        << ",\"method_resolution\":\"runtime-il2cpp-reflection\""
        << ",\"static_pc_rva_authority\":false"
        << ",\"post_state_diff_used\":false"
        << ",\"source\":" << generated_action_source_json(context.source_action)
        << ",\"source_action\":" << action_json(context.source_action)
        << ",\"action_order\":" << context.action_order
        << ",\"source_action_order\":" << context.action_order
        << ",\"effect_id\":"
        << (context.effect_id.empty()
            ? "null"
            : std::string("\"") + json_escape(context.effect_id) + "\"")
        << ",\"effect\":{\"effect_id\":"
        << (context.effect_id.empty()
            ? "null"
            : std::string("\"") + json_escape(context.effect_id) + "\"")
        << ",\"effect_type\":\"ProduceExamEffectType_ExamCardCreateId\""
        << ",\"effect_type_value\":" << context.effect_type
        << ",\"target_card_id\":"
        << (context.target_card_id.empty()
            ? "null"
            : std::string("\"") + json_escape(context.target_card_id) + "\"")
        << ",\"target_upgrade\":" << context.target_upgrade
        << ",\"destination\":\""
        << json_escape(generated_destination_name(context.target_destination))
        << "\",\"destination_type\":" << context.target_destination << "}"
        << ",\"random_state_before\":"
        << (context.random_state_before_known
            ? std::to_string(context.random_state_before) : "null")
        << ",\"random_state_after\":"
        << (context.random_state_after_known
            ? std::to_string(context.random_state_after) : "null")
        << ",\"cards\":[";
    for (std::size_t index = 0; index < card_order.size(); ++index) {
        if (index != 0) out << ',';
        out << generated_card_identity_json(
            context.cards[card_order[index]], context);
    }
    out << "]"
        << ",\"placement\":{\"basis\":\"ExamCardMoveController.AddCard"
           "+ExamEffectCalculateContext.GetRandomInt\",\"calls\":[";
    bool first_call = true;
    for (const GeneratedAddObservation& add : context.adds) {
        for (const GeneratedRandomCall& call : add.random_calls) {
            if (!first_call) out << ',';
            first_call = false;
            out << "{\"minimum\":" << call.minimum
                << ",\"maximum\":" << call.maximum
                << ",\"result\":" << call.result << "}";
        }
    }
    out << "]}"
        << ",\"blockers\":[";
    for (std::size_t index = 0; index < blockers.size(); ++index) {
        if (index != 0) out << ',';
        out << '"' << json_escape(blockers[index]) << '"';
    }
    out << "]}";
    return out.str();
}

bool generated_context_has_unobserved_guid(
    const GeneratedCreateContext& context) noexcept {
    // An empty card set means that no generated identity was observed at all;
    // keep the existing immediate unresolved row for that case.  A pending
    // frame is reserved for an actual card whose lazy GUID boundary can still
    // be observed later.
    if (context.cards.empty()) {
        return false;
    }
    return std::any_of(
        context.cards.begin(),
        context.cards.end(),
        [](const GeneratedCardIdentity& identity) {
            return identity.card != nullptr && !identity.guid_creation_observed;
        });
}

bool generated_context_guids_observed(
    const GeneratedCreateContext& context) noexcept {
    if (context.cards.empty()) {
        return false;
    }
    return std::all_of(
        context.cards.begin(),
        context.cards.end(),
        [](const GeneratedCardIdentity& identity) {
            return identity.card != nullptr && identity.guid_creation_observed;
        });
}

bool generated_context_placement_observed(
    const GeneratedCreateContext& context) noexcept {
    if (context.cards.empty()) {
        return false;
    }
    return std::all_of(
        context.cards.begin(),
        context.cards.end(),
        [](const GeneratedCardIdentity& identity) {
            return identity.card != nullptr && identity.destination_order_known;
        });
}

void emit_generated_context(const GeneratedCreateContext& context) noexcept {
    try {
        const std::string identity_json = generated_create_json(context);
        // The same copied JSON is attached before the action's transition is
        // emitted.  No managed pointer or post-state diff enters this path.
        attach_generated_identity(
            context.sequence, context.action_order, identity_json);
        emit_line(identity_json);
    } catch (...) {
        emit_error("generated-create-context-finalize-failed");
    }
}

void flush_pending_generated_contexts(void* sequence) noexcept {
    if (sequence == nullptr) {
        return;
    }
    for (auto iterator = g_pending_generated_contexts.begin();
         iterator != g_pending_generated_contexts.end();) {
        if (iterator->sequence != sequence) {
            ++iterator;
            continue;
        }
        // A pending frame normally leaves this deque from the GUID hook.  If
        // the serializer never causes CreateGuidIfNeed, emit exactly one
        // unresolved row with the original null GUID and blocker instead of
        // inventing an identity or leaving the frame attached forever.
        GeneratedCreateContext context = std::move(*iterator);
        iterator = g_pending_generated_contexts.erase(iterator);
        emit_generated_context(context);
    }
}

void finalize_pending_generated_context_for_card(
    void* card) noexcept {
    if (card == nullptr) {
        return;
    }
    for (auto iterator = g_pending_generated_contexts.begin();
         iterator != g_pending_generated_contexts.end(); ++iterator) {
        bool matches = false;
        for (const GeneratedCardIdentity& identity : iterator->cards) {
            if (identity.card == card) {
                matches = true;
                break;
            }
        }
        if (!matches) {
            for (const GeneratedAddObservation& add : iterator->adds) {
                if (std::find(add.cards.begin(), add.cards.end(), card) !=
                    add.cards.end()) {
                    matches = true;
                    break;
                }
            }
        }
        if (!matches || !generated_context_guids_observed(*iterator) ||
            !generated_context_placement_observed(*iterator)) {
            continue;
        }
        // Move the fully observed copied frame out before emitting.  The
        // erase guarantees a repeated get_Guid/CreateGuidIfNeed cannot append
        // an exact duplicate to the same action.
        GeneratedCreateContext context = std::move(*iterator);
        g_pending_generated_contexts.erase(iterator);
        emit_generated_context(context);
        return;
    }
}

void observe_generated_guid(
    void* self,
    const std::string& before,
    bool before_readable,
    std::string after,
    bool after_readable) noexcept {
    // Prefer an exact active-frame match, then an exact deferred-frame match;
    // only an active frame may use the pre-AddCard top-frame fallback.
    GeneratedCreateContext* context = generated_context_for_card_exact(self);
    bool pending_context = false;
    if (context == nullptr) {
        context = generated_pending_context_for_card(self);
        pending_context = context != nullptr;
    }
    if (context == nullptr) {
        context = generated_context_for_card(self);
    }
    if (context == nullptr) return;
    try {
        GeneratedCardIdentity& identity = generated_card_for(*context, self);
        identity.guid_readable = before_readable && after_readable;
        identity.guid_was_preexisting = !before.empty();
        identity.guid_creation_observed = before_readable && before.empty() &&
            after_readable && !after.empty();
        identity.guid = std::move(after);
        if (pending_context) {
            finalize_pending_generated_context_for_card(self);
        }
    } catch (...) {
        // A failed observation remains unresolved; no fallback GUID is
        // generated or inferred from a serialized post-state.
    }
}

void generated_card_create_guid_hook(void* self, const MethodInfo* method) noexcept {
    HookGuard guard(kGeneratedCreateGuidHook);
    std::string before;
    std::string after;
    bool before_readable = false;
    if (guard.owner) {
        before_readable = copy_card_guid_field(self, before);
    }
    if (g_generated_create_guid_original != nullptr) {
        g_generated_create_guid_original(self, method);
    }
    if (!guard.owner) return;
    const bool after_readable = copy_card_guid_field(self, after);
    observe_generated_guid(
        self, before, before_readable, std::move(after), after_readable);
}

Il2CppString* generated_card_guid_hook(
    void* self,
    const MethodInfo* method) noexcept {
    HookGuard guard(kGeneratedCardGuidGetterHook);
    std::string before;
    bool before_readable = false;
    if (guard.owner) {
        before_readable = copy_card_guid_field(self, before);
    }
    Il2CppString* result{};
    if (g_generated_card_guid_original != nullptr) {
        result = g_generated_card_guid_original(self, method);
    }
    if (!guard.owner) return result;
    std::string after;
    bool after_readable = copy_card_guid_field(self, after);
    if (after.empty() && result != nullptr) {
        std::string returned;
        if (copy_il2cpp_string(result, returned) && !returned.empty()) {
            after = std::move(returned);
            after_readable = true;
        }
    }
    observe_generated_guid(
        self, before, before_readable, std::move(after), after_readable);
    return result;
}

int generated_random_int_hook(
    void* self,
    int minimum,
    int maximum,
    const MethodInfo* method) noexcept {
    HookGuard guard(kGeneratedRandomIntHook);
    int result = 0;
    if (g_generated_context_random_int_original != nullptr) {
        result = g_generated_context_random_int_original(self, minimum, maximum, method);
    }
    if (!guard.owner) return result;
    GeneratedCreateContext* context = generated_context_for_context(self);
    if (context == nullptr || context->active_add == nullptr) return result;
    try {
        context->active_add->random_calls.push_back({minimum, maximum, result});
    } catch (...) {
    }
    return result;
}

void generated_add_card_prepare(
    GeneratedCreateContext& context,
    GeneratedAddObservation& add,
    void* list,
    int from_position,
    int destination,
    int effect_type,
    void* effect_context) noexcept {
    add.list = list;
    add.context = effect_context;
    add.from_position = from_position;
    add.destination = destination;
    add.effect_type = effect_type;
    add.list_capture_complete = copy_generated_card_list(list, add.cards);
    for (std::size_t index = 0; index < add.cards.size(); ++index) {
        GeneratedCardIdentity& identity = generated_card_for(
            context, add.cards[index], static_cast<int>(index));
        identity.destination = destination;
    }
}

void generated_add_card_finish(
    GeneratedCreateContext& context,
    GeneratedAddObservation& add,
    GeneratedAddObservation* previous) noexcept {
    for (std::size_t index = 0; index < add.cards.size(); ++index) {
        GeneratedCardIdentity& identity = generated_card_for(
            context, add.cards[index], static_cast<int>(index));
        generated_refresh_card_identity(identity);
    }
    context.active_add = previous;
    try {
        context.adds.push_back(std::move(add));
    } catch (...) {
    }
}

void generated_add_card_list_hook(
    void* self,
    void* list,
    int from_position,
    int destination,
    int effect_type,
    void* context_value,
    const MethodInfo* method) noexcept {
    HookGuard guard(kGeneratedAddCardListHook);
    GeneratedCreateContext* context = guard.owner
        ? generated_context_for_context(context_value) : nullptr;
    GeneratedAddObservation add;
    GeneratedAddObservation* previous = nullptr;
    bool prepared = false;
    if (context != nullptr) {
        try {
            generated_add_card_prepare(
                *context, add, list, from_position, destination,
                effect_type, context_value);
            previous = context->active_add;
            context->active_add = &add;
            prepared = true;
        } catch (...) {
            context->active_add = previous;
        }
    }
    if (g_generated_add_card_list_original != nullptr) {
        g_generated_add_card_list_original(
            self, list, from_position, destination, effect_type,
            context_value, method);
    }
    if (prepared) {
        generated_add_card_finish(*context, add, previous);
    }
}

void generated_add_card_single_hook(
    void* self,
    void* card,
    int from_position,
    int destination,
    int effect_type,
    void* context_value,
    const MethodInfo* method) noexcept {
    HookGuard guard(kGeneratedAddCardSingleHook);
    GeneratedCreateContext* context = guard.owner
        ? generated_context_for_context(context_value) : nullptr;
    GeneratedAddObservation add;
    GeneratedAddObservation* previous = nullptr;
    bool prepared = false;
    if (context != nullptr) {
        try {
            generated_add_card_prepare(
                *context, add, nullptr, from_position, destination,
                effect_type, context_value);
            add.cards.clear();
            if (card != nullptr) add.cards.push_back(card);
            add.list_capture_complete = card != nullptr;
            GeneratedCardIdentity& identity = generated_card_for(*context, card, 0);
            identity.destination = destination;
            previous = context->active_add;
            context->active_add = &add;
            prepared = true;
        } catch (...) {
            context->active_add = previous;
        }
    }
    if (g_generated_add_card_single_original != nullptr) {
        g_generated_add_card_single_original(
            self, card, from_position, destination, effect_type,
            context_value, method);
    }
    if (prepared) {
        generated_add_card_finish(*context, add, previous);
    }
}

void generated_card_create_id_hook(
    void* self,
    void* context_value,
    const MethodInfo* method) noexcept {
    HookGuard guard(kGeneratedCreateIdHook);
    bool pushed = false;
    if (guard.owner) {
        try {
            GeneratedCreateContext context;
            context.executor = self;
            context.context = context_value;
            if (g_bindings.generated_context_parameter != nullptr) {
                gkms::il2cpp::call_instance_0(
                    g_bindings.generated_context_parameter,
                    context_value,
                    context.parameter);
            }
            context.sequence = sequence_for_parameter(context.parameter);
            if (g_bindings.generated_context_playing_card != nullptr) {
                gkms::il2cpp::call_instance_0(
                    g_bindings.generated_context_playing_card,
                    context_value,
                    context.playing_card);
            }
            const std::optional<PendingAction> pending = generated_pending_action(
                context.sequence, context.playing_card);
            if (pending.has_value()) {
                context.source_action = pending->action;
                context.source_action_known = true;
                context.action_order = pending->action_order;
            }
            context.effect_known = generated_effect_info(
                context_value, context.effect_id, context.effect_type);
            // Read string fields through a temporary managed pointer; never
            // retain that pointer in the context or writer queue.
            Il2CppString* target_card{};
            if (recorder_read_field(
                    self, g_bindings.generated_create_id_target_card, target_card)) {
                copy_il2cpp_string(target_card, context.target_card_id);
            }
            recorder_read_field(
                self, g_bindings.generated_create_id_target_upgrade,
                context.target_upgrade);
            recorder_read_field(
                self, g_bindings.generated_create_id_move_position_field,
                context.target_destination);
            if (context.parameter != nullptr &&
                g_bindings.generated_parameter_random_state != nullptr) {
                context.random_state_before_known = gkms::il2cpp::call_instance_0(
                    g_bindings.generated_parameter_random_state,
                    context.parameter,
                    context.random_state_before);
            }
            g_generated_contexts.push_back(std::move(context));
            pushed = true;
        } catch (...) {
            emit_error("generated-create-context-setup-failed");
        }
    }
    if (g_generated_create_id_execute_original != nullptr) {
        g_generated_create_id_execute_original(self, context_value, method);
    }
    if (!pushed) return;
    try {
        GeneratedCreateContext& context = g_generated_contexts.back();
        if (context.parameter != nullptr &&
            g_bindings.generated_parameter_random_state != nullptr) {
            context.random_state_after_known = gkms::il2cpp::call_instance_0(
                g_bindings.generated_parameter_random_state,
                context.parameter,
                context.random_state_after);
        }
        generated_assign_placement(context);
        if (generated_context_has_unobserved_guid(context)) {
            // The game may construct/move the card first and invoke its lazy
            // GUID getter only during the next ExamSaveData serialization.
            // Transfer only the copied frame; never call get_Guid here and
            // never use a post-state diff to fill the missing identity.
            context.active_add = nullptr;
            if (g_pending_generated_contexts.size() >=
                kMaximumPendingGeneratedContexts) {
                GeneratedCreateContext oldest = std::move(
                    g_pending_generated_contexts.front());
                g_pending_generated_contexts.pop_front();
                emit_generated_context(oldest);
            }
            g_pending_generated_contexts.push_back(std::move(context));
            g_generated_contexts.pop_back();
            return;
        }
        // All GUIDs were observed in the active frame, so finalize normally.
        // The copied row is attached before any later transition boundary.
        emit_generated_context(context);
        g_generated_contexts.pop_back();
    } catch (...) {
        emit_error("generated-create-context-finalize-failed");
        if (!g_generated_contexts.empty()) g_generated_contexts.pop_back();
    }
}

void emit_settled_diagnostic(
    void* sequence,
    bool is_playing,
    const SnapshotCopy& after,
    const TerminalStatus& terminal,
    const char* reason) noexcept {
    try {
        std::size_t pending_count = 0;
        {
            std::lock_guard lock(g_state_mutex);
            const auto found = g_sequence_capture_states.find(sequence);
            if (found != g_sequence_capture_states.end()) {
                pending_count = found->second.pending_actions.size();
            }
        }
        InvariantStream body;
        body << "{\"record\":\"settled_diagnostic\""
            << ",\"boundary\":\"ExamSequence.set_IsCommandPlaying\""
            << ",\"is_playing\":" << (is_playing ? "true" : "false")
            << ",\"sequence\":" << pointer_json(sequence)
            << ",\"state_after_candidate\":" << snapshot_json(after)
            << ",\"state_after_candidate_captured\":"
            << (after.captured ? "true" : "false")
            << ",\"pending_action_count\":" << pending_count
            << ",\"terminal_candidate\":" << (terminal.terminal() ? "true" : "false")
            << ",\"terminal_known\":"
            << ((terminal.end_exam_known && terminal.completion_known) ? "true" : "false")
            << ",\"commit\":false"
            << ",\"reason\":\""
            << json_escape(reason == nullptr ? "unknown" : reason) << "\"}";
        emit_line(body.str());
    } catch (...) {
    }
}

void emit_action_settlement(
    const PendingActionReceipt& pending,
    const ActionSettlementState& state,
    bool transition_ready,
    const char* status) noexcept {
    try {
        InvariantStream body;
        body << "{\"record\":\"action_settlement\""
            << ",\"schema\":\"" << kActionSettlementSchema << "\""
            << ",\"runtime_sequence\":" << pointer_json(pending.sequence)
            << ",\"action_order\":" << pending.action_order
            << ",\"official_action_order\":";
        if (pending.official_action_captured) {
            body << pending.official_action_order;
        } else {
            body << "null";
        }
        body << ",\"action\":" << action_json(pending.action)
            << ",\"official_action_captured\":"
            << (pending.official_action_captured ? "true" : "false")
            << ",\"official_action_duplicate\":"
            << (pending.official_action_duplicate ? "true" : "false")
            << ",\"queue_drain_observed\":"
            << (pending.queue_drain_observed ? "true" : "false")
            << ",\"physically_settled\":"
            << (state.physically_drained() ? "true" : "false")
            << ",\"stable_main_decision\":"
            << (state.stable_main_decision() ? "true" : "false")
            << ",\"transition_ready\":"
            << (transition_ready ? "true" : "false")
            << ",\"phase\":";
        if (state.phase_known) body << state.phase; else body << "null";
        body << ",\"is_command_playing\":";
        if (state.command_playing_known) {
            body << (state.command_playing ? "true" : "false");
        } else {
            body << "null";
        }
        body << ",\"command_stack_empty\":";
        if (state.command_stack_empty_known) {
            body << (state.command_stack_empty ? "true" : "false");
        } else {
            body << "null";
        }
        body << ",\"is_turn_card_play_end\":";
        if (state.turn_card_play_end_known) {
            body << (state.turn_card_play_end ? "true" : "false");
        } else {
            body << "null";
        }
        body << ",\"terminal\":"
            << (state.terminal.terminal() ? "true" : "false")
            << ",\"boundary\":\"ExamSequence.set_IsCommandPlaying(false)-after\""
            << ",\"status\":\""
            << json_escape(status == nullptr ? "unknown" : status) << "\"}";
        emit_line(body.str());
    } catch (...) {
    }
}

void emit_incomplete_transition_retirement(
    const PendingAction& pending,
    const ActionSettlementState& current,
    const char* reason) noexcept {
    try {
        InvariantStream body;
        body << "{\"record\":\"transition_incomplete\""
            << ",\"schema\":\"" << kActionSettlementSchema << "\""
            << ",\"runtime_sequence\":" << pointer_json(pending.sequence)
            << ",\"action_order\":" << pending.action_order
            << ",\"official_action_order\":";
        if (pending.official_action_captured) {
            body << pending.official_action_order;
        } else {
            body << "null";
        }
        body << ",\"action\":" << action_json(pending.action)
            << ",\"official_action_captured\":"
            << (pending.official_action_captured ? "true" : "false")
            << ",\"queue_drain_observed\":"
            << (pending.queue_drain_observed ? "true" : "false")
            << ",\"current_stable_main_decision\":"
            << (current.stable_main_decision() ? "true" : "false")
            << ",\"retired\":true"
            << ",\"state_after\":null"
            << ",\"reason\":\""
            << json_escape(reason == nullptr ? "unknown" : reason) << "\"}";
        emit_line(body.str());
    } catch (...) {
    }
}

void emit_transition(
    const PendingAction& pending,
    const SnapshotCopy& after,
    const TerminalStatus& terminal,
    const char* boundary,
    const char* pairing_reason) noexcept {
    try {
        const bool same_thread = pending.managed_thread_id == GetCurrentThreadId();
        const bool state_before_copied = pending.before.captured;
        const bool state_after_copied = after.captured;
        const bool terminal_known =
            terminal.end_exam_known && terminal.completion_known;
        bool verified_complete = false;
        const bool action_state_pairing_candidate = false;
        const bool transition_promotion_candidate = false;
        InvariantStream body;
        body << "{\"record\":\"transition\""
            << ",\"source\":\"managed-runtime-hook\""
            << ",\"surface\":\"live-or-replay\""
            << ",\"runtime_sequence\":" << pointer_json(pending.sequence)
            << ",\"action_order\":" << pending.action_order
            << ",\"official_action_order\":";
        if (pending.official_action_captured) {
            body << pending.official_action_order;
        } else {
            body << "null";
        }
        body << ",\"official_action_captured\":"
            << (pending.official_action_captured ? "true" : "false")
            << ",\"official_action_duplicate\":"
            << (pending.official_action_duplicate ? "true" : "false")
            << ",\"official_action_authority\":"
            << (pending.official_action_captured
                ? "\"ExamParameterModel.AddPlayLog(ExamPlayLog)\""
                : "null")
            << ",\"action\":" << action_json(pending.action)
            << ",\"state_before\":" << snapshot_json(pending.before)
            << ",\"state_before_captured\":"
            << (state_before_copied ? "true" : "false")
            << ",\"state_before_complete\":false"
            << ",\"state_after\":" << snapshot_json(after)
            << ",\"state_after_captured\":"
            << (state_after_copied ? "true" : "false")
            << ",\"state_after_complete\":false"
            << ",\"legal_actions\":{\"complete\":false"
            << ",\"legal_actions_complete\":false"
            << ",\"exact\":false,\"actions\":null"
            << ",\"source\":\"same-ExamSequence-runtime\""
            << ",\"reason\":\"candidate-enumeration-shadow-only\"}"
            << ",\"generated_card_identity\":";
        if (pending.generated_card_identities.empty()) {
            body << "null";
        } else if (pending.generated_card_identities.size() == 1) {
            body << pending.generated_card_identities.front();
        } else {
            body << '[';
            for (std::size_t index = 0;
                 index < pending.generated_card_identities.size(); ++index) {
                if (index != 0) body << ',';
                body << pending.generated_card_identities[index];
            }
            body << ']';
        }
#if defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || \
    defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE)
            // Candidate output is intentionally separate from the
            // authoritative legal_actions field.  It is always provisional;
            // consumers must keep exact=false/complete=false until the
            // runtime probes promote each family.
        body << ",\"legal_action_candidates\":"
             << legal_candidate_json(pending.legal_candidates);
#endif
        body << ",\"terminal\":" << (terminal.terminal() ? "true" : "false")
            << ",\"terminal_known\":"
            << (terminal_known ? "true" : "false")
            << ",\"terminal_predicate\":"
            << (terminal.end_exam ? "true" : "false")
            << ",\"terminal_completion\":"
            << (terminal.completion ? "true" : "false")
            << ",\"boundary\":\"" << json_escape(boundary) << "\""
            << ",\"pairing\":\"shadow\""
            << ",\"pairing_reason\":\"" << json_escape(pairing_reason) << "\""
            << ",\"managed_thread_match\":" << (same_thread ? "true" : "false")
            << ",\"action_state_exact\":false"
            << ",\"action_state_pairing_candidate\":"
            << (action_state_pairing_candidate ? "true" : "false")
            << ",\"transition_promotion_candidate\":"
            << (transition_promotion_candidate ? "true" : "false")
            << ",\"transition_promotion_gate\":"
               "\"python-stage-89-field-continuity-v1\"}"
            ;
        emit_line(body.str(), false, verified_complete);
    } catch (...) {
    }
}

bool take_pending_for_transition(
    void* sequence,
    std::uint64_t expected_action_order,
    PendingAction& output) noexcept {
    try {
        std::lock_guard lock(g_state_mutex);
        const auto found = g_sequence_capture_states.find(sequence);
        if (found == g_sequence_capture_states.end() ||
            found->second.pending_actions.empty()) {
            return false;
        }
        PendingAction& pending = found->second.pending_actions.front();
        if (pending.action_order != expected_action_order ||
            !pending.official_action_captured ||
            !pending.queue_drain_observed) {
            return false;
        }
        output = std::move(pending);
        found->second.pending_actions.pop_front();
        return true;
    } catch (...) {
        return false;
    }
}

void on_command_drain_after(void* sequence) noexcept {
    if (sequence == nullptr) {
        return;
    }

    // Clone/forecast sequences reach the same setter.  Do not even invoke
    // settlement getters unless this exact sequence owns a pending manual
    // transaction established by the existing known-manual registration
    // gate.
    try {
        std::lock_guard lock(g_state_mutex);
        const auto found = g_sequence_capture_states.find(sequence);
        if (found == g_sequence_capture_states.end() ||
            found->second.pending_actions.empty()) {
            return;
        }
    } catch (...) {
        emit_error("action-settlement-pending-lookup-failed");
        return;
    }

    const ActionSettlementState state =
        capture_action_settlement_state(sequence);
    PendingActionReceipt observed;
    bool found_pending = false;
    bool emit_physical_evidence = false;
    try {
        std::lock_guard lock(g_state_mutex);
        const auto found = g_sequence_capture_states.find(sequence);
        if (found != g_sequence_capture_states.end() &&
            !found->second.pending_actions.empty()) {
            PendingAction& pending = found->second.pending_actions.front();
            found_pending = true;
            if (state.physically_drained()) {
                pending.queue_drain_observed = true;
                if (pending.official_action_captured &&
                    !pending.queue_drain_evidence_emitted) {
                    pending.queue_drain_evidence_emitted = true;
                    emit_physical_evidence = true;
                }
            }
            observed = pending_action_receipt(pending);
        }
    } catch (...) {
        emit_error("action-settlement-pending-update-failed");
        return;
    }
    if (!found_pending) {
        return;
    }

    const SnapshotCopy no_snapshot;
    if (!state.physically_drained()) {
        emit_settled_diagnostic(
            sequence,
            false,
            no_snapshot,
            state.terminal,
            "tracked-pending-command-queue-not-drained");
        return;
    }
    if (!observed.official_action_captured) {
        emit_settled_diagnostic(
            sequence,
            false,
            no_snapshot,
            state.terminal,
            "queue-drained-before-official-action-bound");
        return;
    }

    const bool ordinary_action =
        observed.action.play_type == 2 || observed.action.play_type == 3;
    const bool transition_ready =
        ordinary_action && state.stable_main_decision();
    if (emit_physical_evidence) {
        emit_action_settlement(
            observed,
            state,
            transition_ready,
            transition_ready
                ? "queue-drained-stable-main"
                : "queue-drained-awaiting-stable-main-or-terminal");
    }

    if (!transition_ready) {
        emit_settled_diagnostic(
            sequence,
            false,
            no_snapshot,
            state.terminal,
            "queue-drained-awaiting-stable-main-or-terminal");
        return;
    }

    // Snapshot on the managed hook thread only after the official action and
    // physical drain milestones are both proven.  The serializer may
    // materialize a lazy generated-card GUID, so flush/attach that copied
    // identity before removing the one pending parent.
    const SnapshotCopy after = capture_snapshot(sequence);
    flush_pending_generated_contexts(sequence);
    if (!after.captured) {
        emit_settled_diagnostic(
            sequence,
            false,
            after,
            state.terminal,
            "stable-main-state-after-snapshot-failed");
        return;
    }
    PendingAction completed;
    if (!take_pending_for_transition(
            sequence, observed.action_order, completed)) {
        emit_error("stable-main-pending-finalizer-mismatch");
        return;
    }
    emit_transition(
        completed,
        after,
        state.terminal,
        "ExamSequence.set_IsCommandPlaying(false)-after",
        "same-action-queue-drain-stable-main");
}

void emit_terminal_marker(
    void* sequence,
    const char* marker,
    const TerminalStatus& status) noexcept;

void finish_pending_at_terminal(
    void* sequence,
    const SnapshotCopy& after,
    const TerminalStatus& status,
    const char* reason) noexcept {
    if (sequence == nullptr) {
        return;
    }
    // Terminal snapshots can be the last managed call that materializes a
    // lazy GUID.  Resolve/emit the generated context before taking the
    // pending action so its attached copy is present in the transition.
    flush_pending_generated_contexts(sequence);
    PendingAction pending;
    bool found = false;
    {
        std::lock_guard lock(g_state_mutex);
        const auto state = g_sequence_capture_states.find(sequence);
        if (state != g_sequence_capture_states.end() &&
            !state->second.pending_actions.empty()) {
            pending = std::move(state->second.pending_actions.front());
            found = true;
        }
        // A terminal ExamSequence owns no future action parent.  Clear both
        // its pending state and every parameter identity even when no pending
        // row was found, so a reused managed pointer cannot inherit a stale
        // registration from an incomplete stage.
        if (state != g_sequence_capture_states.end()) {
            g_sequence_capture_states.erase(state);
        }
        for (auto parameter = g_parameter_sequences.begin();
             parameter != g_parameter_sequences.end();) {
            if (parameter->second == sequence) {
                parameter = g_parameter_sequences.erase(parameter);
            } else {
                ++parameter;
            }
        }
    }
    if (!found) {
        return;
    }
    emit_transition(pending, after, status, "terminal-marker", reason);
}

void emit_terminal_marker(
    void* sequence,
    const char* marker,
    const TerminalStatus& status) noexcept {
    if (sequence == nullptr || g_terminal_marker_depth != 0) {
        return;
    }
    ++g_terminal_marker_depth;
    const SnapshotCopy after = capture_snapshot(sequence);
    try {
        InvariantStream body;
        body << "{\"record\":\"terminal_marker\""
            << ",\"marker\":\"" << json_escape(marker) << "\""
            << ",\"sequence\":" << pointer_json(sequence)
            << ",\"state\":" << snapshot_json(after)
            << ",\"state_captured\":" << (after.captured ? "true" : "false")
            << ",\"is_end_exam\":"
            << (status.end_exam ? "true" : "false")
            << ",\"is_exam_end_complete\":"
            << (status.completion ? "true" : "false")
            << ",\"terminal\":" << (status.terminal() ? "true" : "false")
            << ",\"terminal_known\":"
            << ((status.end_exam_known && status.completion_known) ? "true" : "false")
            << "}";
        emit_line(body.str());
    } catch (...) {
    }
    if (status.terminal()) {
        finish_pending_at_terminal(
            sequence,
            after,
            status,
            "terminal-completion-marker");
    }
    --g_terminal_marker_depth;
}

void on_command_added_before(void* sequence, void* command) noexcept {
    if (sequence == nullptr || command == nullptr) {
        return;
    }
    const ActionIdentity identity = forget_command(command);
    if (!identity.known) {
        try {
            InvariantStream body;
            body << "{\"record\":\"unidentified_command\""
                << ",\"boundary\":\"ExamSequence.AddExecuteCommand\""
                << ",\"sequence\":" << pointer_json(sequence)
                << ",\"command\":" << pointer_json(command)
                << ",\"reason\":\"factory-identity-missing\"}";
            emit_line(body.str());
        } catch (...) {
        }
        return;
    }

    // Internal trigger/draw/effect commands (isManual=false) stay inside the
    // currently buffered manual action and never become separate actions.
    if (!identity.is_manual) {
        try {
            InvariantStream body;
            body << "{\"record\":\"internal_command\""
                << ",\"boundary\":\"ExamSequence.AddExecuteCommand\""
                << ",\"action\":" << action_json(identity)
                << ",\"command\":" << pointer_json(command)
                << ",\"reason\":\"isManual-false-kept-inside-manual-action\"}";
            emit_line(body.str());
        } catch (...) {
        }
        return;
    }

    // A parameter enters the official action spine only when this sequence
    // has a known manual action.  Recommendation evaluation uses cloned
    // sequences/parameters and only emits internal or unidentified commands;
    // looking up the parameter after the manual gate keeps those clones
    // unregistered.  The mapping is committed with the pending parent below,
    // under the same mutex, before the original AddExecuteCommand can call
    // AddPlayLog.
    void* runtime_parameter = parameter_for_sequence(sequence);

    // This manual enqueue is a new Main decision boundary.  Its snapshot is
    // always the new action's state_before.  It may close the prior action
    // only when that same pending transaction already proved both official
    // acceptance and physical queue drain; merely seeing another command is
    // never sufficient evidence for the prior S'.
    const ActionSettlementState decision_state =
        capture_action_settlement_state(sequence);
    SnapshotCopy decision_snapshot = capture_snapshot(sequence);
    PendingAction next;
    next.action = identity;
    next.command = command;
    next.sequence = sequence;
    next.managed_thread_id = GetCurrentThreadId();
    next.action_order = ++g_action_order;
    next.before = decision_snapshot;
#if defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || \
    defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE)
    // The candidate set is sampled at the same managed decision boundary as
    // state_before.  All getters run synchronously on this thread; only the
    // copied JSON representation is retained by the writer queue.
    next.legal_candidates = enumerate_legal_candidates(sequence);
    const SnapshotCopy post_enumeration_snapshot = capture_snapshot(sequence);
    record_candidate_purity(
        next.legal_candidates,
        decision_snapshot,
        post_enumeration_snapshot);
    emit_candidate_boundary(sequence, next.action_order, next.legal_candidates);
#endif
    // The decision snapshot above is where the managed serializer may lazily
    // invoke CreateGuidIfNeed.  Give that hook a chance to attach an exact
    // generated row, then flush any still-unobserved frame before the prior
    // action is emitted below.
    flush_pending_generated_contexts(sequence);
    PendingAction previous;
    bool has_previous = false;
    bool previous_transition_ready = false;
    try {
        std::lock_guard lock(g_state_mutex);
        auto& queue = g_sequence_capture_states[sequence].pending_actions;
        if (!queue.empty()) {
            previous = std::move(queue.front());
            queue.pop_front();
            has_previous = true;
            previous_transition_ready =
                previous.official_action_captured &&
                previous.queue_drain_observed &&
                decision_state.stable_main_decision() &&
                decision_snapshot.captured;
        }
        if (queue.size() >= kMaximumPendingActionsPerSequence) {
            queue.pop_front();
        }
        queue.push_back(std::move(next));
        if (runtime_parameter != nullptr) {
            g_parameter_sequences[runtime_parameter] = sequence;
        }
    } catch (...) {
        emit_error("pending-action-native-queue-failed");
        return;
    }
    if (has_previous) {
        if (previous_transition_ready) {
            emit_transition(
                previous,
                decision_snapshot,
                decision_state.terminal,
                "ExamSequence.AddExecuteCommand(next-main-decision-before)",
                "prior-official-action-drained-next-main-decision");
        } else {
            const char* reason =
                !previous.official_action_captured
                ? "next-manual-before-previous-official-action-unbound"
                : !previous.queue_drain_observed
                    ? "next-manual-before-previous-queue-drain-unobserved"
                    : !decision_state.stable_main_decision()
                        ? "next-manual-before-not-stable-main-decision"
                        : "next-main-decision-state-after-snapshot-failed";
            emit_incomplete_transition_retirement(
                previous, decision_state, reason);
        }
    }
}

void* create_use_hand_hook(
    int index,
    bool is_manual,
    void* card,
    const MethodInfo* method) {
    HookGuard guard(kFactoryHandHook);
    void* command = g_create_use_hand_original == nullptr ? nullptr :
        g_create_use_hand_original(index, is_manual, card, method);
    if (guard.owner) {
        ActionIdentity identity = hand_identity(index, is_manual);
        copy_source_card_identity(identity, card);
        remember_command(command, identity);
        emit_factory("ExamPlayCommand.CreateUseHandCommand", identity, command);
    }
    return command;
}

void* create_use_drink_hook(
    int index,
    bool is_manual,
    void* drink,
    const MethodInfo* method) {
    HookGuard guard(kFactoryDrinkHook);
    void* command = g_create_use_drink_original == nullptr ? nullptr :
        g_create_use_drink_original(index, is_manual, drink, method);
    if (guard.owner) {
        ActionIdentity identity = drink_identity(index, is_manual);
        identity.source_drink_id = generic_string_value(drink, "get_Id");
        remember_command(command, identity);
        emit_factory("ExamPlayCommand.CreateUseDrinkCommand", identity, command);
    }
    return command;
}

void* create_turn_end_hook(bool is_manual, const MethodInfo* method) {
    HookGuard guard(kFactoryTurnEndHook);
    void* command = g_create_turn_end_original == nullptr ? nullptr :
        g_create_turn_end_original(is_manual, method);
    if (guard.owner) {
        const ActionIdentity identity = turn_end_identity(is_manual);
        remember_command(command, identity);
        emit_factory("ExamPlayCommand.CreateTurnEndCommand", identity, command);
    }
    return command;
}

void add_play_log_hook(
    void* parameter,
    void* log,
    const MethodInfo* method) {
    HookGuard guard(kAddPlayLogHook);
    if (g_add_play_log_original != nullptr) {
        g_add_play_log_original(parameter, log, method);
    }
    if (!guard.owner || log == nullptr) {
        return;
    }
    bool is_select = false;
    if (!gkms::il2cpp::call_instance_0(
            g_bindings.play_log_is_select, log, is_select)) {
        emit_error("captured-action-log-kind-unavailable");
        return;
    }
    if (is_select) {
        void* select_indexes{};
        std::vector<int> copied_indexes;
        if (!gkms::il2cpp::call_instance_0(
                g_bindings.play_log_select_indexes, log, select_indexes) ||
            !copy_int32_array(select_indexes, copied_indexes)) {
            emit_error("effect-card-select-index-copy-failed");
            return;
        }
        capture_effect_card_select(parameter, std::move(copied_indexes));
        return;
    }
    void* command{};
    if (gkms::il2cpp::call_instance_0(
            g_bindings.play_log_command, log, command) &&
        command != nullptr) {
        capture_main_action(parameter, command);
    }
}

void add_execute_command_hook(
    void* self,
    void* command,
    const MethodInfo* method) {
    HookGuard guard(kAddExecuteHook);
    if (guard.owner) {
        // This is intentionally before the original enqueue mutation.  The
        // copy is made on the managed caller thread and only its UTF-8 text
        // enters the native writer queue.
        on_command_added_before(self, command);
    }
    if (g_add_execute_command_original != nullptr) {
        g_add_execute_command_original(self, command, method);
    }
}

void set_is_command_playing_hook(
    void* self,
    bool value,
    const MethodInfo* method) {
    HookGuard guard(kSetPlayingHook);
    if (g_set_is_command_playing_original != nullptr) {
        g_set_is_command_playing_original(self, value, method);
    }
    if (guard.owner && !value) {
        // The shared Live/Replay queue has returned to not-playing.  The
        // handler proves stack drain before publishing physical settlement;
        // only an ordinary hand/drink already at a stable Main decision may
        // also finalize a training state here.  Phase-changing actions remain
        // on the same pending transaction until next Main or terminal.
        on_command_drain_after(self);
    }
}

bool is_end_exam_hook(void* self, const MethodInfo* method) {
    HookGuard guard(kIsEndExamHook);
    bool result = false;
    if (g_is_end_exam_original != nullptr) {
        result = g_is_end_exam_original(self, method);
    }
    if (guard.owner && result) {
        const TerminalStatus status = terminal_status(self);
        emit_terminal_marker(self, "ExamSequence.IsEndExam", status);
    }
    return result;
}

bool is_exam_end_complete_hook(void* self, const MethodInfo* method) {
    HookGuard guard(kGetEndCompleteHook);
    bool result = false;
    if (g_is_exam_end_complete_original != nullptr) {
        result = g_is_exam_end_complete_original(self, method);
    }
    if (guard.owner && result) {
        const void* sequence = sequence_for_parameter(self);
        if (sequence != nullptr) {
            const TerminalStatus status = terminal_status(const_cast<void*>(sequence));
            emit_terminal_marker(
                const_cast<void*>(sequence),
                "ExamParameterModel.get_IsExamEndComplete",
                status);
        } else {
            try {
                InvariantStream body;
                body << "{\"record\":\"terminal_marker\""
                    << ",\"marker\":\"ExamParameterModel.get_IsExamEndComplete\""
                    << ",\"sequence\":null"
                    << ",\"state\":null"
                    << ",\"state_captured\":false"
                    << ",\"terminal\":false"
                    << ",\"terminal_known\":false"
                    << ",\"reason\":\"sequence-association-missing\"}";
                emit_line(body.str());
            } catch (...) {
            }
        }
    }
    return result;
}

void set_exam_end_complete_hook(void* self, const MethodInfo* method) {
    HookGuard guard(kSetEndCompleteHook);
    if (g_set_exam_end_complete_original != nullptr) {
        g_set_exam_end_complete_original(self, method);
    }
    if (guard.owner) {
        const void* sequence = sequence_for_parameter(self);
        if (sequence != nullptr) {
            const TerminalStatus status = terminal_status(const_cast<void*>(sequence));
            emit_terminal_marker(
                const_cast<void*>(sequence),
                "ExamParameterModel.SetExamEndComplete",
                status);
        }
    }
}

bool is_executable_pointer(void* value) noexcept {
    if (value == nullptr) {
        return false;
    }
    MEMORY_BASIC_INFORMATION region{};
    if (VirtualQuery(value, &region, sizeof(region)) != sizeof(region) ||
        region.State != MEM_COMMIT || (region.Protect & PAGE_GUARD) != 0) {
        return false;
    }
    const DWORD protection = region.Protect & 0xFF;
    return protection == PAGE_EXECUTE || protection == PAGE_EXECUTE_READ ||
        protection == PAGE_EXECUTE_READWRITE || protection == PAGE_EXECUTE_WRITECOPY;
}

struct HookResult {
    std::string name;
    void* target{};
    std::uint32_t expected_token{};
    std::uint32_t source_token{};
    std::uint32_t actual_token{};
    std::uint32_t expected_parameter_count{};
    std::uint32_t actual_parameter_count{};
    bool installed{};
    bool enabled{};
    std::string reason;
};

template <typename T>
HookResult install_hook(
    const char* name,
    MethodInfo* method,
    std::uint32_t expected_token,
    std::uint32_t expected_parameter_count,
    void* detour,
    T* original) {
    HookResult result;
    result.name = name == nullptr ? "unknown" : name;
    result.source_token = expected_token;
    result.expected_token = expected_token;
    result.expected_parameter_count = expected_parameter_count;
    result.target = method == nullptr ? nullptr : method->method_pointer;
    if (method == nullptr || result.target == nullptr) {
        result.reason = "method-not-found";
        return result;
    }
    expected_token = method_binding_token(method, expected_parameter_count, expected_token);
    result.expected_token = expected_token;
    if (!expected_token) {
        result.reason = "pc-method-profile-binding-missing";
        return result;
    }
    result.actual_token = g_api.method_token(method);
    result.actual_parameter_count = g_api.method_parameter_count(method);
    if (result.actual_token != expected_token) {
        result.reason = "method-token-mismatch";
        return result;
    }
    if (result.actual_parameter_count != expected_parameter_count) {
        result.reason = "method-parameter-count-mismatch";
        return result;
    }
    if (!is_executable_pointer(result.target)) {
        result.reason = "method-pointer-not-executable";
        return result;
    }
    const MH_STATUS status = MH_CreateHook(
        result.target,
        detour,
        reinterpret_cast<void**>(original));
    result.installed = status == MH_OK;
    result.reason = result.installed ? "" : MH_StatusToString(status);
    return result;
}

struct MethodCheck {
    const char* name{};
    MethodInfo* method{};
    std::uint32_t expected_token{};
    std::uint32_t source_token{};
    std::uint32_t expected_parameter_count{};
    std::uint32_t actual_token{};
    std::uint32_t actual_parameter_count{};
    bool valid{};
    std::string reason;
};

MethodCheck verify_method(
    const char* name,
    MethodInfo* method,
    std::uint32_t expected_token,
    std::uint32_t expected_parameter_count) {
    MethodCheck result;
    result.name = name;
    result.method = method;
    result.source_token = expected_token;
    result.expected_token = expected_token;
    result.expected_parameter_count = expected_parameter_count;
    if (method == nullptr || method->method_pointer == nullptr) {
        result.reason = "method-not-found";
        return result;
    }
    expected_token = method_binding_token(method, expected_parameter_count, expected_token);
    result.expected_token = expected_token;
    if (!expected_token) {
        result.reason = "pc-method-profile-binding-missing";
        return result;
    }
    result.actual_token = g_api.method_token(method);
    result.actual_parameter_count = g_api.method_parameter_count(method);
    if (result.actual_token != expected_token) {
        result.reason = "method-token-mismatch";
        return result;
    }
    if (result.actual_parameter_count != expected_parameter_count) {
        result.reason = "method-parameter-count-mismatch";
        return result;
    }
    if (!is_executable_pointer(method->method_pointer)) {
        result.reason = "method-pointer-not-executable";
        return result;
    }
    result.valid = true;
    return result;
}

std::string method_checks_json(const std::vector<MethodCheck>& checks) {
    InvariantStream out;
    out << "[";
    for (std::size_t index = 0; index < checks.size(); ++index) {
        if (index != 0) {
            out << ',';
        }
        const MethodCheck& check = checks[index];
        out << "{\"name\":\"" << json_escape(check.name == nullptr ? "" : check.name)
            << "\",\"expected_token\":" << check.expected_token
            << ",\"source_token\":" << check.source_token
            << ",\"actual_token\":" << check.actual_token
            << ",\"expected_parameter_count\":"
            << check.expected_parameter_count
            << ",\"actual_parameter_count\":"
            << check.actual_parameter_count
            << ",\"method\":" << pointer_json(check.method)
            << ",\"valid\":" << (check.valid ? "true" : "false")
            << ",\"reason\":\"" << json_escape(check.reason) << "\"}";
    }
    out << "]";
    return out.str();
}

std::string hook_results_json(const std::vector<HookResult>& results) {
    InvariantStream out;
    out << "[";
    for (std::size_t index = 0; index < results.size(); ++index) {
        if (index != 0) {
            out << ',';
        }
        const HookResult& result = results[index];
        out << "{\"name\":\"" << json_escape(result.name)
            << "\",\"target\":" << pointer_json(result.target)
            << ",\"expected_token\":" << result.expected_token
            << ",\"source_token\":" << result.source_token
            << ",\"actual_token\":" << result.actual_token
            << ",\"expected_parameter_count\":"
            << result.expected_parameter_count
            << ",\"actual_parameter_count\":"
            << result.actual_parameter_count
            << ",\"installed\":" << (result.installed ? "true" : "false")
            << ",\"enabled\":" << (result.enabled ? "true" : "false")
            << ",\"reason\":\"" << json_escape(result.reason) << "\"}";
    }
    out << "]";
    return out.str();
}

void rollback_hooks(const std::vector<HookResult>& results) noexcept {
    g_stopping = true;
    for (const HookResult& result : results) {
        if (result.installed && result.target != nullptr) {
            MH_DisableHook(result.target);
        }
    }
    const auto deadline = std::chrono::steady_clock::now() +
        std::chrono::milliseconds(2000);
    while (g_active_hooks.load() != 0 &&
        std::chrono::steady_clock::now() < deadline) {
        Sleep(1);
    }
    for (const HookResult& result : results) {
        if (result.installed && result.target != nullptr) {
            MH_RemoveHook(result.target);
        }
    }
    if (g_minhook_owner) {
        MH_Uninitialize();
        g_minhook_owner = false;
    }
}

bool resolve_bindings() {
    g_bindings = {};
    g_bindings.sequence_class = g_api.find_class(
        kAssembly, kExamNamespace, "ExamSequence");
    g_bindings.parameter_class = g_api.find_class(
        kAssembly, kExamNamespace, "ExamParameterModel");
    g_bindings.play_command_class = g_api.find_class(
        kAssembly, kExamNamespace, "ExamPlayCommand");
    g_bindings.save_class = g_api.find_class(
        kAssembly, kExamNamespace, "ExamSaveData");
    g_bindings.json_class = g_api.find_class(
        kJsonAssembly, kJsonNamespace, "JsonUtility");
    g_bindings.effect_context_class = g_api.find_class(
        kAssembly, kExamNamespace, "ExamEffectCalculateContext");
    g_bindings.card_class = g_api.find_class(
        kAssembly, "Campus.InGame.Card", "ExamCardData");
    g_bindings.card_move_controller_class = g_api.find_class(
        kAssembly, "Campus.InGame.Card", "ExamCardMoveController");
    g_bindings.card_create_id_class = g_api.find_class(
        kAssembly, kExamNamespace, "CardCreateIdEffectExecutor");
    g_bindings.command_stack_class = g_api.find_class(
        kAssembly, "Campus.InGame", "ExamCommandStack");
    if (g_bindings.sequence_class == nullptr ||
        g_bindings.parameter_class == nullptr ||
        g_bindings.play_command_class == nullptr ||
        g_bindings.save_class == nullptr ||
        g_bindings.json_class == nullptr ||
        g_bindings.effect_context_class == nullptr ||
        g_bindings.card_class == nullptr ||
        g_bindings.card_move_controller_class == nullptr ||
        g_bindings.card_create_id_class == nullptr ||
        g_bindings.command_stack_class == nullptr) {
        return false;
    }

    g_bindings.create_use_hand = g_api.find_method(
        g_bindings.play_command_class, "CreateUseHandCommand", 3);
    g_bindings.create_use_drink = g_api.find_method(
        g_bindings.play_command_class, "CreateUseDrinkCommand", 3);
    g_bindings.create_turn_end = g_api.find_method(
        g_bindings.play_command_class, "CreateTurnEndCommand", 1);
    // ExamSequence has two one-argument overloads: the public single-command
    // boundary (0x06004E58) and the private IEnumerable overload
    // (0x06004E59).  Name/arity lookup is therefore ambiguous and can return
    // the wrong method depending on metadata enumeration order.  Resolve the
    // exact single-command method by token before the atomic hook preflight;
    // otherwise the recorder could fail closed even though the required
    // boundary is present (or, worse, attach to the enumerable entry if a
    // future preflight check were weakened).
    g_bindings.add_execute_command = find_method_by_token(
        g_bindings.sequence_class,
        "AddExecuteCommand",
        0x06004E58,
        1);
    g_bindings.set_is_command_playing = g_api.find_method(
        g_bindings.sequence_class, "set_IsCommandPlaying", 1);
    g_bindings.sequence_parameter = g_api.find_method(
        g_bindings.sequence_class, "get_Parameter", 0);
    g_bindings.is_end_exam = g_api.find_method(
        g_bindings.sequence_class, "IsEndExam", 0);
    g_bindings.is_exam_end_complete = g_api.find_method(
        g_bindings.parameter_class, "get_IsExamEndComplete", 0);
    g_bindings.set_exam_end_complete = g_api.find_method(
        g_bindings.parameter_class, "SetExamEndComplete", 0);
    g_bindings.exam_save_ctor = g_api.find_method(
        g_bindings.save_class, ".ctor", 2);
    g_bindings.json_to_json = g_api.find_method(
        g_bindings.json_class, "ToJson", 2);
    g_bindings.object_new = reinterpret_cast<void*>(GetProcAddress(
        g_api.game_assembly(), "il2cpp_object_new"));
    g_bindings.exam_play_log_class = g_api.find_class(
        kAssembly, kExamNamespace, "ExamPlayLog");
    g_bindings.main_action_log_ctor = find_method_by_token(
        g_bindings.exam_play_log_class,
        ".ctor",
        0x06004FBF,
        2);
    g_bindings.effect_card_select_log_ctor = find_method_by_token(
        g_bindings.exam_play_log_class,
        ".ctor",
        0x06004FC0,
        2);
    g_bindings.add_play_log = find_method_by_token(
        g_bindings.parameter_class,
        "AddPlayLog",
        0x06004D30,
        1);
    g_bindings.play_log_select_indexes = find_method_by_token(
        g_bindings.exam_play_log_class,
        "get_SelectIndex",
        0x06004FB6,
        0);
    g_bindings.play_log_is_select = find_method_by_token(
        g_bindings.exam_play_log_class,
        "get_IsSelectLog",
        0x06004FB7,
        0);
    g_bindings.play_log_command = find_method_by_token(
        g_bindings.exam_play_log_class,
        "get_Command",
        0x06004FBB,
        0);

    g_bindings.candidate_parameter_phase = g_api.find_method(
        g_bindings.parameter_class, "get_Phase", 0);
    g_bindings.candidate_sequence_command_playing = g_api.find_method(
        g_bindings.sequence_class, "get_IsCommandPlaying", 0);
    g_bindings.candidate_parameter_turn_card_play_end = g_api.find_method(
        g_bindings.parameter_class, "get_IsTurnCardPlayEnd", 0);
    g_bindings.candidate_sequence_command_stack = g_api.find_method(
        g_bindings.sequence_class, "get_CommandStack", 0);
    g_bindings.candidate_command_stack_is_empty = g_api.find_method(
        g_bindings.command_stack_class, "get_IsEmpty", 0);

    g_bindings.generated_context_parameter = g_api.find_method(
        g_bindings.effect_context_class, "get_ExamParameter", 0);
    g_bindings.generated_context_playing_card = g_api.find_method(
        g_bindings.effect_context_class, "get_PlayingCard", 0);
    g_bindings.generated_context_playing_effect = g_api.find_method(
        g_bindings.effect_context_class, "get_PlayingEffect", 0);
    g_bindings.generated_context_random_int = g_api.find_method(
        g_bindings.effect_context_class, "GetRandomInt", 2);
    g_bindings.generated_parameter_random_state = g_api.find_method(
        g_bindings.parameter_class, "get_RandomState", 0);
    g_bindings.generated_create_id_execute = g_api.find_method(
        g_bindings.card_create_id_class, "ExecuteEffect", 1);
    g_bindings.generated_card_guid = g_api.find_method(
        g_bindings.card_class, "get_Guid", 0);
    g_bindings.generated_card_id = g_api.find_method(
        g_bindings.card_class, "get_Id", 0);
    g_bindings.generated_card_upgrade = g_api.find_method(
        g_bindings.card_class, "get_UpgradeCount", 0);
    g_bindings.generated_card_create_guid = g_api.find_method(
        g_bindings.card_class, "CreateGuidIfNeed", 0);
    g_bindings.generated_add_card_single = find_method_by_token(
        g_bindings.card_move_controller_class,
        "AddCard",
        0x060063B7,
        5);
    g_bindings.generated_add_card_list = find_method_by_token(
        g_bindings.card_move_controller_class,
        "AddCard",
        0x060063B8,
        5);
    g_bindings.generated_card_guid_field = g_api.find_field(
        g_bindings.card_class, "_guid");
    g_bindings.generated_create_id_target_card = g_api.find_field(
        g_bindings.card_create_id_class, "_targetCardId");
    g_bindings.generated_create_id_target_upgrade = g_api.find_field(
        g_bindings.card_create_id_class, "_targetUpgradeCount");
    g_bindings.generated_create_id_move_position_field = g_api.find_field(
        g_bindings.card_create_id_class, "_movePositionType");
    g_il2cpp_array_length = reinterpret_cast<il2cpp_array_length_fn>(
        GetProcAddress(g_api.game_assembly(), "il2cpp_array_length"));
    g_il2cpp_array_get_byte_length =
        reinterpret_cast<il2cpp_array_get_byte_length_fn>(GetProcAddress(
            g_api.game_assembly(), "il2cpp_array_get_byte_length"));
    g_il2cpp_array_object_header_size =
        reinterpret_cast<il2cpp_array_object_header_size_fn>(GetProcAddress(
            g_api.game_assembly(), "il2cpp_array_object_header_size"));
#if defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || \
    defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE)
    g_il2cpp_class_get_rank = reinterpret_cast<il2cpp_class_get_rank_fn>(
        GetProcAddress(g_api.game_assembly(), "il2cpp_class_get_rank"));
    g_bindings.card_utility_class = g_api.find_class(
        kAssembly, kExamNamespace, "ExamCardUtility");
    g_bindings.drink_class = g_api.find_class(
        kAssembly, "Campus.InGame.Drink", "ProduceDrinkData");
    g_bindings.candidate_sequence_hand_list = g_api.find_method(
        g_bindings.sequence_class, "get_HandList", 0);
    g_bindings.candidate_create_effect_resolver = g_api.find_method(
        g_bindings.sequence_class, "CreateEffectResolver", 0);
    g_bindings.candidate_context_hand_list = g_api.find_method(
        g_bindings.effect_context_class, "get_HandList", 0);
    g_bindings.candidate_validate_use_hand_card = g_api.find_method(
        g_bindings.card_utility_class, "ValidateUseHandCard", 2);
    g_bindings.candidate_context_dispose = g_api.find_method(
        g_bindings.effect_context_class, "Dispose", 0);
    g_bindings.candidate_parameter_drink_list = g_api.find_method(
        g_bindings.parameter_class, "get_DrinkList", 0);
    g_bindings.candidate_drink_effect_list = g_api.find_method(
        g_bindings.drink_class, "get_ProduceEffectList", 0);
    g_bindings.candidate_drink_id = g_api.find_method(
        g_bindings.drink_class, "get_Id", 0);

    // IReadOnlyList<T> may be backed by either List<T> (Replay restore) or a
    // rank-1 managed array (fresh Live).  candidate_list_count handles arrays
    // through the pinned IL2CPP exports above; keep List<T> only as the
    // non-array fallback and still prefer concrete inflated methods.
    constexpr const char* kListAssemblies[] = {
        "mscorlib", "System", "System.Private.CoreLib"};
    for (const char* assembly_name : kListAssemblies) {
        void* list_class = g_api.find_class(
            assembly_name, "System.Collections.Generic", "List`1");
        if (list_class == nullptr) {
            continue;
        }
        g_bindings.candidate_list_count = g_api.find_method(
            list_class, "get_Count", 0);
        g_bindings.candidate_list_item = g_api.find_method(
            list_class, "get_Item", 1);
        if (g_bindings.candidate_list_count != nullptr &&
            g_bindings.candidate_list_item != nullptr) {
            break;
        }
        g_bindings.candidate_list_count = nullptr;
        g_bindings.candidate_list_item = nullptr;
    }
#endif
    return g_bindings.create_use_hand != nullptr &&
        g_bindings.create_use_drink != nullptr &&
        g_bindings.create_turn_end != nullptr &&
        g_bindings.add_execute_command != nullptr &&
        g_bindings.set_is_command_playing != nullptr &&
        g_bindings.sequence_parameter != nullptr &&
        g_bindings.is_end_exam != nullptr &&
        g_bindings.is_exam_end_complete != nullptr &&
        g_bindings.set_exam_end_complete != nullptr &&
        g_bindings.exam_save_ctor != nullptr &&
        g_bindings.json_to_json != nullptr &&
        g_bindings.object_new != nullptr &&
        g_bindings.effect_context_class != nullptr &&
        g_bindings.card_class != nullptr &&
        g_bindings.card_move_controller_class != nullptr &&
        g_bindings.card_create_id_class != nullptr &&
        g_bindings.generated_context_parameter != nullptr &&
        g_bindings.generated_context_playing_card != nullptr &&
        g_bindings.generated_context_playing_effect != nullptr &&
        g_bindings.generated_context_random_int != nullptr &&
        g_bindings.generated_parameter_random_state != nullptr &&
        g_bindings.generated_create_id_execute != nullptr &&
        g_bindings.generated_card_guid != nullptr &&
        g_bindings.generated_card_id != nullptr &&
        g_bindings.generated_card_upgrade != nullptr &&
        g_bindings.generated_card_create_guid != nullptr &&
        g_bindings.generated_add_card_single != nullptr &&
        g_bindings.generated_add_card_list != nullptr &&
        g_bindings.generated_card_guid_field != nullptr &&
        g_bindings.generated_create_id_target_card != nullptr &&
        g_bindings.generated_create_id_target_upgrade != nullptr &&
        g_bindings.generated_create_id_move_position_field != nullptr &&
        g_il2cpp_array_length != nullptr &&
        g_il2cpp_array_get_byte_length != nullptr &&
        g_il2cpp_array_object_header_size != nullptr &&
        g_bindings.exam_play_log_class != nullptr &&
        g_bindings.main_action_log_ctor != nullptr &&
        g_bindings.effect_card_select_log_ctor != nullptr &&
        g_bindings.add_play_log != nullptr &&
        g_bindings.play_log_select_indexes != nullptr &&
        g_bindings.play_log_is_select != nullptr &&
        g_bindings.play_log_command != nullptr &&
        g_bindings.command_stack_class != nullptr &&
        g_bindings.candidate_parameter_phase != nullptr &&
        g_bindings.candidate_sequence_command_playing != nullptr &&
        g_bindings.candidate_parameter_turn_card_play_end != nullptr &&
        g_bindings.candidate_sequence_command_stack != nullptr &&
        g_bindings.candidate_command_stack_is_empty != nullptr
#if defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || \
    defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE)
        && g_bindings.card_utility_class != nullptr
        && g_bindings.effect_context_class != nullptr
        && g_bindings.drink_class != nullptr
        && g_bindings.candidate_sequence_hand_list != nullptr
        && g_bindings.candidate_create_effect_resolver != nullptr
        && g_bindings.candidate_context_hand_list != nullptr
        && g_bindings.candidate_validate_use_hand_card != nullptr
        && g_bindings.candidate_context_dispose != nullptr
        && g_bindings.candidate_parameter_drink_list != nullptr
        && g_bindings.candidate_drink_effect_list != nullptr
        && g_bindings.candidate_drink_id != nullptr
#endif
        ;
}

bool game_module_header(MODULEINFO& info, DWORD& timestamp) noexcept {
    if (!GetModuleInformation(
        GetCurrentProcess(),
        g_api.game_assembly(),
        &info,
        sizeof(info)) ||
        info.lpBaseOfDll == nullptr ||
        info.SizeOfImage < sizeof(IMAGE_DOS_HEADER)) {
        return false;
    }
    const auto base = reinterpret_cast<std::uintptr_t>(info.lpBaseOfDll);
    __try {
        const auto* dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
        if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew <= 0 ||
            static_cast<DWORD>(dos->e_lfanew) >
                info.SizeOfImage - sizeof(IMAGE_NT_HEADERS64)) {
            return false;
        }
        const auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(
            base + static_cast<std::uintptr_t>(dos->e_lfanew));
        if (nt->Signature != IMAGE_NT_SIGNATURE ||
            nt->FileHeader.Machine != IMAGE_FILE_MACHINE_AMD64 ||
            nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) {
            return false;
        }
        timestamp = nt->FileHeader.TimeDateStamp;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

std::string module_fingerprint_json() {
    MODULEINFO info{};
    DWORD timestamp{};
    if (!game_module_header(info, timestamp)) {
        return "{\"valid\":false}";
    }
    InvariantStream out;
    out << "{\"valid\":true"
        << ",\"pid\":" << GetCurrentProcessId()
        << ",\"game_assembly_base\":" << pointer_json(g_api.game_assembly())
        << ",\"game_assembly_size\":" << info.SizeOfImage
        << ",\"pe_timestamp\":" << timestamp
        << ",\"source_version_image_size\":" << kExpectedGameAssemblyImageSize
        << ",\"source_version_pe_timestamp\":" << kExpectedGameAssemblyTimestamp
        << ",\"verified_pc_identity\":" << g_verified_pc_identity.dump()
        << "}";
    return out.str();
}

std::filesystem::path output_path(HMODULE module) {
    (void)module;
    const auto root = gkms::public_runtime_paths::state_root();
    std::filesystem::path directory = root / L"telemetry";
    std::error_code error;
    std::filesystem::create_directories(directory, error);
    return directory / (
        L"runtime_exam_recorder_shadow_" +
        std::to_wstring(GetCurrentProcessId()) + L".jsonl");
}

void close_output() noexcept {
    std::lock_guard lock(g_output_mutex);
    if (g_output.is_open()) {
        g_output.flush();
        g_output.close();
    }
}

DWORD fail_start(DWORD error_code, const char* reason) noexcept {
    emit_error(reason);
    stop_writer();
    close_output();
    g_start_state = StartState::failed;
    g_stopping = true;
    bootstrap_marker("start-failed");
    return error_code;
}

}  // namespace

DWORD WINAPI start_on_managed_thread(void* module) {
    bootstrap_marker("start-enter");
    StartState expected = StartState::idle;
    if (!g_start_state.compare_exchange_strong(expected, StartState::running)) {
        if (expected == StartState::ready) {
            return ERROR_SUCCESS;
        }
        return expected == StartState::running ? ERROR_BUSY : ERROR_INVALID_STATE;
    }

    g_module = static_cast<HMODULE>(module);
    if (g_module == nullptr) {
        g_module = GetModuleHandleW(L"gkms_runtime_exam_recorder.dll");
    }
    g_stopping = false;
    std::filesystem::path path;
    try {
        path = output_path(g_module);
    } catch (...) {
        g_start_state = StartState::failed;
        g_stopping = true;
        return ERROR_PATH_NOT_FOUND;
    }
    g_output.open(path, std::ios::out | std::ios::app | std::ios::binary);
    if (!g_output.is_open()) {
        g_start_state = StartState::failed;
        g_stopping = true;
        bootstrap_marker("output-open-failed");
        return ERROR_OPEN_FAILED;
    }
    if (!start_writer()) {
        close_output();
        g_start_state = StartState::failed;
        g_stopping = true;
        bootstrap_marker("writer-start-failed");
        return ERROR_NOT_ENOUGH_MEMORY;
    }

    emit_line(
        "{\"record\":\"preflight_begin\",\"candidate\":\"runtime_exam_recorder\"}");
    bootstrap_marker("api-initialize-begin");
    if (!g_api.initialize() || !g_api.ready() || g_api.domain() == nullptr) {
        emit_error(
            g_api.ready() && g_api.current_thread() == nullptr ?
            "caller-thread-not-managed" : "il2cpp-api-unavailable");
        return fail_start(ERROR_DLL_INIT_FAILED, "managed-runtime-preflight-failed");
    }
    bootstrap_marker("api-initialize-end");

    try {
        gkms::bridge::verify_current_pc_method_profile();
        g_verified_pc_identity = gkms::bridge::verified_pc_binding_identity();
        g_verified_pc_identity["identity_source"] =
            "runtime_exam_recorder managed preflight: actual AMD64 PE and full stable metadata hash";
        emit_line(nlohmann::json{{"record","pc_method_profile_preflight"},{"valid",true},
            {"identity",g_verified_pc_identity},{"calculation_equivalence_claimed",false},
            {"model_compatibility_claimed",false},{"hooks_installed",false}}.dump());
    } catch (const std::exception& error) {
        emit_error(error.what());
        return fail_start(ERROR_BAD_EXE_FORMAT, "pc-version-method-profile-preflight-failed");
    } catch (...) {
        return fail_start(ERROR_BAD_EXE_FORMAT, "pc-version-method-profile-preflight-failed");
    }
    bootstrap_marker("module-fingerprint-ready");

    if (!g_field_api.initialize(g_api.game_assembly()))
        return fail_start(ERROR_PROC_NOT_FOUND, "public-field-layout-exports-unavailable");

    if (!resolve_bindings()) {
        return fail_start(ERROR_PROC_NOT_FOUND, "managed-binding-resolution-failed");
    }

    // Snapshot calls are not detoured, but they are part of the same fail-
    // closed preflight: if the constructor/serializer ABI is not the pinned
    // current method, no action hooks are allowed to run.
    const std::vector<MethodCheck> snapshot_checks{
        verify_method(
            "ExamSaveData..ctor",
            g_bindings.exam_save_ctor,
            0x06004DE3,
            2),
        verify_method(
            "UnityEngine.JsonUtility.ToJson",
            g_bindings.json_to_json,
            0x06000004,
            2),
        verify_method(
            "ExamSequence.get_Parameter",
            g_bindings.sequence_parameter,
            0x06004E18,
            0),
    };
    const bool snapshots_ready = std::all_of(
        snapshot_checks.begin(), snapshot_checks.end(),
        [](const MethodCheck& check) { return check.valid; });
    if (!is_executable_pointer(g_bindings.object_new)) {
        emit_line(
            "{\"record\":\"preflight\",\"valid\":false"
            ",\"reason\":\"il2cpp_object_new-not-executable\""
            ",\"snapshot_methods\":" + method_checks_json(snapshot_checks) + "}");
        return fail_start(ERROR_PROC_NOT_FOUND, "snapshot-preflight-failed");
    }
    if (!snapshots_ready) {
        emit_line(
            "{\"record\":\"preflight\",\"valid\":false"
            ",\"reason\":\"snapshot-method-token-or-arity-mismatch\""
            ",\"snapshot_methods\":" + method_checks_json(snapshot_checks) + "}");
        return fail_start(ERROR_BAD_EXE_FORMAT, "snapshot-preflight-failed");
    }

    const std::vector<MethodCheck> action_settlement_checks{
        verify_method(
            "ExamParameterModel.get_Phase",
            g_bindings.candidate_parameter_phase,
            0x06004C55,
            0),
        verify_method(
            "ExamSequence.get_IsCommandPlaying",
            g_bindings.candidate_sequence_command_playing,
            0x06004E29,
            0),
        verify_method(
            "ExamParameterModel.get_IsTurnCardPlayEnd",
            g_bindings.candidate_parameter_turn_card_play_end,
            0x06004C73,
            0),
        verify_method(
            "ExamSequence.get_CommandStack",
            g_bindings.candidate_sequence_command_stack,
            0x06004E2F,
            0),
        verify_method(
            "ExamCommandStack.get_IsEmpty",
            g_bindings.candidate_command_stack_is_empty,
            0x060015CD,
            0),
    };
    const bool action_settlement_ready = std::all_of(
        action_settlement_checks.begin(),
        action_settlement_checks.end(),
        [](const MethodCheck& check) { return check.valid; });
    if (!action_settlement_ready) {
        emit_line(
            "{\"record\":\"preflight\",\"valid\":false"
            ",\"reason\":\"action-settlement-method-mismatch\""
            ",\"methods\":" + method_checks_json(action_settlement_checks) +
            "}");
        return fail_start(
            ERROR_BAD_EXE_FORMAT, "action-settlement-preflight-failed");
    }
    emit_line(
        "{\"record\":\"action_settlement_preflight\",\"valid\":true"
        ",\"schema\":\"" + std::string(kActionSettlementSchema) + "\""
        ",\"physical_boundary\":\"ExamSequence.set_IsCommandPlaying(false)-after\""
        ",\"methods\":" + method_checks_json(action_settlement_checks) + "}");

    const std::vector<MethodCheck> captured_action_checks{
        verify_method(
            "ExamPlayLog..ctor(ExamPlayCommand,ExamEffectCalculateContext)",
            g_bindings.main_action_log_ctor,
            0x06004FBF,
            2),
        verify_method(
            "ExamPlayLog..ctor(int[],ExamEffectCalculateContext)",
            g_bindings.effect_card_select_log_ctor,
            0x06004FC0,
            2),
        verify_method(
            "ExamParameterModel.AddPlayLog(ExamPlayLog)",
            g_bindings.add_play_log,
            0x06004D30,
            1),
        verify_method(
            "ExamPlayLog.get_SelectIndex",
            g_bindings.play_log_select_indexes,
            0x06004FB6,
            0),
        verify_method(
            "ExamPlayLog.get_IsSelectLog",
            g_bindings.play_log_is_select,
            0x06004FB7,
            0),
        verify_method(
            "ExamPlayLog.get_Command",
            g_bindings.play_log_command,
            0x06004FBB,
            0),
    };
    const bool captured_action_spine_ready =
        g_il2cpp_array_length != nullptr &&
        g_il2cpp_array_get_byte_length != nullptr &&
        g_il2cpp_array_object_header_size != nullptr &&
        std::all_of(
            captured_action_checks.begin(),
            captured_action_checks.end(),
            [](const MethodCheck& check) { return check.valid; });
    if (!captured_action_spine_ready) {
        emit_line(
            "{\"record\":\"preflight\",\"valid\":false"
            ",\"reason\":\"captured-action-spine-hook-mismatch\""
            ",\"methods\":" + method_checks_json(captured_action_checks) +
            "}");
        return fail_start(ERROR_BAD_EXE_FORMAT, "captured-action-spine-preflight-failed");
    }
    emit_line(
        "{\"record\":\"captured_action_spine_preflight\",\"valid\":true"
        ",\"schema\":\"" + std::string(kCapturedActionSchema) + "\""
        ",\"official_action_authority\":\"ExamParameterModel.AddPlayLog(single)\""
        ",\"main_origin\":\"ExamPlayLog..ctor(command,context)\""
        ",\"secondary_origin\":\"ExamPlayLog..ctor(selectIndex,context)\""
        ",\"commit_boundary\":\"ExamParameterModel.AddPlayLog(ExamPlayLog)\""
        ",\"live_replay_shared_boundary\":true"
        ",\"chosen_action_exact\":true"
        ",\"state_pair_exact\":false"
        ",\"legal_actions_complete\":false"
        ",\"methods\":" + method_checks_json(captured_action_checks) + "}");

    // Generated-card identity is a separate fail-closed surface.  The
    // AddCard overloads share an arity, so the list overload is resolved by
    // enumerating MethodInfo and checking token 0x060063B8.  No Android RVA
    // is accepted as a substitute for a current executable pointer.
    const std::vector<MethodCheck> generated_guid_checks{
        verify_method(
            "ExamEffectCalculateContext.get_ExamParameter",
            g_bindings.generated_context_parameter,
            0x06004411,
            0),
        verify_method(
            "ExamEffectCalculateContext.get_PlayingCard",
            g_bindings.generated_context_playing_card,
            0x0600442D,
            0),
        verify_method(
            "ExamEffectCalculateContext.get_PlayingEffect",
            g_bindings.generated_context_playing_effect,
            0x06004431,
            0),
        verify_method(
            "ExamEffectCalculateContext.GetRandomInt",
            g_bindings.generated_context_random_int,
            0x06004414,
            2),
        verify_method(
            "ExamParameterModel.get_RandomState",
            g_bindings.generated_parameter_random_state,
            0x06004CDB,
            0),
        verify_method(
            "CardCreateIdEffectExecutor.ExecuteEffect",
            g_bindings.generated_create_id_execute,
            0x060045B3,
            1),
        verify_method(
            "ExamCardData.get_Guid",
            g_bindings.generated_card_guid,
            0x060062B9,
            0),
        verify_method(
            "ExamCardData.CreateGuidIfNeed",
            g_bindings.generated_card_create_guid,
            0x060062BA,
            0),
        verify_method(
            "ExamCardData.get_Id",
            g_bindings.generated_card_id,
            0x060062BD,
            0),
        verify_method(
            "ExamCardData.get_UpgradeCount",
            g_bindings.generated_card_upgrade,
            0x060062C5,
            0),
        verify_method(
            "ExamCardMoveController.AddCard(single)",
            g_bindings.generated_add_card_single,
            0x060063B7,
            5),
        verify_method(
            "ExamCardMoveController.AddCard(list)",
            g_bindings.generated_add_card_list,
            0x060063B8,
            5),
    };
    const bool generated_guid_fields_ready =
        recorder_field_usable<void*>(g_bindings.generated_card_guid_field) &&
        recorder_field_usable<void*>(g_bindings.generated_create_id_target_card) &&
        recorder_field_usable<std::int32_t>(g_bindings.generated_create_id_target_upgrade) &&
        recorder_field_usable<std::int32_t>(g_bindings.generated_create_id_move_position_field);
    const bool generated_guid_ready = generated_guid_fields_ready &&
        std::all_of(
            generated_guid_checks.begin(), generated_guid_checks.end(),
            [](const MethodCheck& check) { return check.valid; });
    if (!generated_guid_ready) {
        emit_line(
            "{\"record\":\"preflight\",\"valid\":false"
            ",\"reason\":\"generated-guid-method-or-field-mismatch\""
            ",\"generated_guid_methods\":" +
            method_checks_json(generated_guid_checks) +
            ",\"generated_guid_fields\":{\"card_guid\":" +
            recorder_field_diagnostic(g_bindings.generated_card_guid_field) +
            ",\"target_card\":" +
            recorder_field_diagnostic(g_bindings.generated_create_id_target_card) +
            ",\"target_upgrade\":" +
            recorder_field_diagnostic(g_bindings.generated_create_id_target_upgrade) +
            ",\"target_destination\":" +
            recorder_field_diagnostic(g_bindings.generated_create_id_move_position_field) + "}}");
        return fail_start(ERROR_BAD_EXE_FORMAT, "generated-guid-preflight-failed");
    }
    emit_line(
        "{\"record\":\"generated_guid_preflight\",\"valid\":true"
        ",\"identity_schema\":\"gkms.runtime-generated-card-identity.v1\""
        ",\"method_resolution\":\"runtime-il2cpp-reflection\""
        ",\"static_pc_rva_authority\":false"
        ",\"exact_requires\":[\"CreateGuidIfNeed\",\"AddCard\","
        "\"GetRandomInt\",\"action_order\"]"
        ",\"post_state_diff_used\":false"
        ",\"methods\":" + method_checks_json(generated_guid_checks) + "}");

#if defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || \
    defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE)
    // This target has a stricter candidate-surface gate than the deployed
    // recorder.  Every method used by the getter-only projection must resolve
    // to the current metadata token, expected arity, and executable pointer
    // before any action hook can emit a candidate row.  The hand validator is
    // intentionally only bound here; its ValueTuple ABI/purity probe remains
    // blocked and therefore never contributes to authoritative legal_actions.
    const std::vector<MethodCheck> legal_candidate_checks{
        verify_method(
            "ExamSequence.get_HandList",
            g_bindings.candidate_sequence_hand_list,
            0x06004E1D,
            0),
        verify_method(
            "ExamSequence.CreateEffectResolver",
            g_bindings.candidate_create_effect_resolver,
            0x06004E37,
            0),
        verify_method(
            "ExamEffectCalculateContext.get_HandList",
            g_bindings.candidate_context_hand_list,
            0x06004417,
            0),
        verify_method(
            "ExamCardUtility.ValidateUseHandCard",
            g_bindings.candidate_validate_use_hand_card,
            0x060043FB,
            2),
        verify_method(
            "ExamEffectCalculateContext.Dispose",
            g_bindings.candidate_context_dispose,
            0x0600445E,
            0),
        verify_method(
            "ExamParameterModel.get_DrinkList",
            g_bindings.candidate_parameter_drink_list,
            0x06004C83,
            0),
        verify_method(
            "ProduceDrinkData.get_ProduceEffectList",
            g_bindings.candidate_drink_effect_list,
            0x06004169,
            0),
        verify_method(
            "ProduceDrinkData.get_Id",
            g_bindings.candidate_drink_id,
            0x06004160,
            0),
    };
    const bool legal_candidates_ready = action_settlement_ready && std::all_of(
        legal_candidate_checks.begin(), legal_candidate_checks.end(),
        [](const MethodCheck& check) { return check.valid; });
    const bool generic_list_fallback_ready =
        candidate_method_usable(g_bindings.candidate_list_count) &&
        candidate_method_usable(g_bindings.candidate_list_item);
    if (!legal_candidates_ready) {
        emit_line(
            "{\"record\":\"preflight\",\"valid\":false"
            ",\"reason\":\"legal-candidate-method-token-or-arity-mismatch\""
            ",\"legal_candidate_methods\":" +
            method_checks_json(legal_candidate_checks) +
            ",\"generic_list_fallback_ready\":" +
            (generic_list_fallback_ready ? "true" : "false") + "}");
        return fail_start(ERROR_BAD_EXE_FORMAT, "legal-candidate-preflight-failed");
    }
    emit_line(
        "{\"record\":\"legal_candidate_preflight\",\"valid\":true"
        ",\"metadata_sha256\":\"" + std::string(kExpectedMetadataSha256) +
        "\",\"candidate_schema\":\"gkms.runtime-exam-legal-action-candidates.shadow.v1\""
        ",\"authoritative_legal_actions\":null"
        ",\"legal_actions_complete\":false"
        ",\"exact\":false"
        ",\"hand\":{\"bound\":true,\"probe_status\":\"blocked\","
        "\"reason\":\"current-pc-use-hand-validator-purity-and-abi-unverified\"}"
        ",\"hand_identity\":{\"source\":\"ExamSequence.get_HandList\","
        "\"guid_source\":\"ExamCardData._guid-read-only\","
        "\"lazy_guid_getter_called\":false}"
        ",\"drink\":{\"bound\":true,\"source\":\"getter-only\","
        "\"body_equivalence\":\"unverified\"}"
        ",\"end_turn\":{\"bound\":true,\"source\":\"getter-only\","
        "\"body_equivalence\":\"unverified\"}"
        ",\"generic_list_fallback_ready\":" +
        (generic_list_fallback_ready ? "true" : "false") +
        ",\"methods\":" + method_checks_json(legal_candidate_checks) + "}");
#endif

    const MH_STATUS initialize_status = MH_Initialize();
    if (initialize_status == MH_OK) {
        g_minhook_owner = true;
    } else if (initialize_status != MH_ERROR_ALREADY_INITIALIZED) {
        emit_error("minhook-initialize-failed");
        return fail_start(ERROR_DLL_INIT_FAILED, "minhook-initialize-failed");
    }

    std::vector<HookResult> hooks;
    hooks.reserve(15);
    hooks.push_back(install_hook(
        "ExamPlayCommand.CreateUseHandCommand",
        g_bindings.create_use_hand,
        0x06004DCE,
        3,
        reinterpret_cast<void*>(&create_use_hand_hook),
        &g_create_use_hand_original));
    hooks.push_back(install_hook(
        "ExamPlayCommand.CreateUseDrinkCommand",
        g_bindings.create_use_drink,
        0x06004DD0,
        3,
        reinterpret_cast<void*>(&create_use_drink_hook),
        &g_create_use_drink_original));
    hooks.push_back(install_hook(
        "ExamPlayCommand.CreateTurnEndCommand",
        g_bindings.create_turn_end,
        0x06004DD9,
        1,
        reinterpret_cast<void*>(&create_turn_end_hook),
        &g_create_turn_end_original));
    hooks.push_back(install_hook(
        "ExamSequence.AddExecuteCommand",
        g_bindings.add_execute_command,
        0x06004E58,
        1,
        reinterpret_cast<void*>(&add_execute_command_hook),
        &g_add_execute_command_original));
    hooks.push_back(install_hook(
        "ExamParameterModel.AddPlayLog(ExamPlayLog)",
        g_bindings.add_play_log,
        0x06004D30,
        1,
        reinterpret_cast<void*>(&add_play_log_hook),
        &g_add_play_log_original));
    hooks.push_back(install_hook(
        "ExamSequence.set_IsCommandPlaying",
        g_bindings.set_is_command_playing,
        0x06004E2A,
        1,
        reinterpret_cast<void*>(&set_is_command_playing_hook),
        &g_set_is_command_playing_original));
    hooks.push_back(install_hook(
        "ExamSequence.IsEndExam",
        g_bindings.is_end_exam,
        0x06004E44,
        0,
        reinterpret_cast<void*>(&is_end_exam_hook),
        &g_is_end_exam_original));
    hooks.push_back(install_hook(
        "ExamParameterModel.get_IsExamEndComplete",
        g_bindings.is_exam_end_complete,
        0x06004C5E,
        0,
        reinterpret_cast<void*>(&is_exam_end_complete_hook),
        &g_is_exam_end_complete_original));
    hooks.push_back(install_hook(
        "ExamParameterModel.SetExamEndComplete",
        g_bindings.set_exam_end_complete,
        0x06004D04,
        0,
        reinterpret_cast<void*>(&set_exam_end_complete_hook),
        &g_set_exam_end_complete_original));
    hooks.push_back(install_hook(
        "CardCreateIdEffectExecutor.ExecuteEffect",
        g_bindings.generated_create_id_execute,
        0x060045B3,
        1,
        reinterpret_cast<void*>(&generated_card_create_id_hook),
        &g_generated_create_id_execute_original));
    hooks.push_back(install_hook(
        "ExamCardData.get_Guid",
        g_bindings.generated_card_guid,
        0x060062B9,
        0,
        reinterpret_cast<void*>(&generated_card_guid_hook),
        &g_generated_card_guid_original));
    hooks.push_back(install_hook(
        "ExamCardData.CreateGuidIfNeed",
        g_bindings.generated_card_create_guid,
        0x060062BA,
        0,
        reinterpret_cast<void*>(&generated_card_create_guid_hook),
        &g_generated_create_guid_original));
    hooks.push_back(install_hook(
        "ExamCardMoveController.AddCard(single)",
        g_bindings.generated_add_card_single,
        0x060063B7,
        5,
        reinterpret_cast<void*>(&generated_add_card_single_hook),
        &g_generated_add_card_single_original));
    hooks.push_back(install_hook(
        "ExamCardMoveController.AddCard(list)",
        g_bindings.generated_add_card_list,
        0x060063B8,
        5,
        reinterpret_cast<void*>(&generated_add_card_list_hook),
        &g_generated_add_card_list_original));
    hooks.push_back(install_hook(
        "ExamEffectCalculateContext.GetRandomInt",
        g_bindings.generated_context_random_int,
        0x06004414,
        2,
        reinterpret_cast<void*>(&generated_random_int_hook),
        &g_generated_context_random_int_original));

    const bool all_created = std::all_of(
        hooks.begin(), hooks.end(),
        [](const HookResult& hook) { return hook.installed; });
    if (!all_created) {
        emit_line(
            "{\"record\":\"preflight\",\"valid\":false"
            ",\"reason\":\"hook-create-failed\",\"hooks\":" +
            hook_results_json(hooks) + "}");
        rollback_hooks(hooks);
        return fail_start(ERROR_PROC_NOT_FOUND, "hook-create-failed");
    }

    bool enable_ok = true;
    for (HookResult& hook : hooks) {
        const MH_STATUS status = MH_EnableHook(hook.target);
        if (status != MH_OK) {
            hook.reason = MH_StatusToString(status);
            enable_ok = false;
            break;
        }
        hook.enabled = true;
    }
    if (!enable_ok) {
        emit_line(
            "{\"record\":\"preflight\",\"valid\":false"
            ",\"reason\":\"hook-enable-failed\",\"hooks\":" +
            hook_results_json(hooks) + "}");
        rollback_hooks(hooks);
        return fail_start(ERROR_DLL_INIT_FAILED, "hook-enable-failed");
    }

    emit_line(
        "{\"record\":\"preflight\",\"valid\":true"
        ",\"atomic_hooks\":true"
        ",\"state_snapshot_source\":\"ExamSaveData(sequence,false)+JsonUtility.ToJson(obj,false)\""
        ",\"state_snapshot_completeness\":\"unverified\""
        ",\"legal_actions_complete\":false"
        ",\"exact\":false"
        ",\"module\":" + module_fingerprint_json() +
        ",\"hooks\":" + hook_results_json(hooks) + "}");
    g_start_state = StartState::ready;
    bootstrap_marker("start-ready");
    return ERROR_SUCCESS;
}

}  // namespace gkms::runtime_exam_recorder
