#include "recorder.hpp"

#include <Windows.h>

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(module);
    }
    return TRUE;
}

extern "C" __declspec(dllexport) DWORD WINAPI
GKMSRuntimeExamRecorderStartOnManagedThread(void* module) {
    return gkms::runtime_exam_recorder::start_on_managed_thread(module);
}

// Compatibility names let an already-installed managed bootstrap invoke this
// standalone candidate through the same registered-thread call path.  They
// are aliases only; no remote-thread or controller bootstrap is added here.
extern "C" __declspec(dllexport) DWORD WINAPI
GKMSObserverStartOnManagedThread(void* module) {
    return GKMSRuntimeExamRecorderStartOnManagedThread(module);
}

extern "C" __declspec(dllexport) unsigned int
GKMSRuntimeExamRecorderProtocolVersion() {
    return 1;
}

extern "C" __declspec(dllexport) unsigned int
GKMSObserverProtocolVersion() {
    // Existing protocol-3 managed bootstrap accepts this compatibility
    // surface.  The recorder's own shadow JSONL schema remains v1 above.
    return 3;
}

