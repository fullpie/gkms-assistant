// Public build of the original once bootstrap, shared by text/Awake and the
// guarded managed PlayerLoop callback. Only module/protocol selection changes;
// research/outer/probe modules are not loaded.
#include "gkmsTelemetryBootstrap.hpp"
#include "public_runtime_paths.hpp"
#include <Windows.h>
#include <atomic>
#include <cstdio>

namespace {
enum class StartState { idle, running, ready, failed };
std::atomic<StartState> start_state{StartState::idle};
}

void try_start_gkms_telemetry_on_managed_thread() {
    StartState expected=StartState::idle;
    if(!start_state.compare_exchange_strong(expected,StartState::running))return;
    HMODULE recorder{};
    try {
        const auto path=gkms::public_runtime_paths::native_root()/L"gkms_runtime_exam_recorder.dll";
        recorder=LoadLibraryExW(path.c_str(),nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
        if(!recorder){start_state=StartState::failed;std::printf("GKMS recorder load failed (%lu).\n",GetLastError());return;}
        using protocol_fn=unsigned int(*)();
        using start_fn=DWORD(WINAPI*)(void*);
        const auto protocol=reinterpret_cast<protocol_fn>(GetProcAddress(recorder,"GKMSRuntimeExamRecorderProtocolVersion"));
        const auto start=reinterpret_cast<start_fn>(GetProcAddress(recorder,"GKMSRuntimeExamRecorderStartOnManagedThread"));
        if(!protocol||protocol()!=1||!start){
            start_state=StartState::failed;
            // No native hook has been entered, so this pre-start rejection can unload.
            FreeLibrary(recorder);
            std::printf("GKMS recorder export/protocol verification failed.\n");return;
        }
        const DWORD result=start(recorder);
        start_state=result==0?StartState::ready:StartState::failed;
        std::printf("GKMS recorder managed-thread start returned %lu.\n",result);
        // Never unload after entering a recorder, including a partial start.
    } catch (...) {
        start_state=StartState::failed;
        std::printf("GKMS recorder portable path/start failed.\n");
    }
}
