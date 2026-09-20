#pragma once

#include <Windows.h>

namespace gkms::runtime_exam_recorder {

// This entry point must be called synchronously from a registered IL2CPP
// managed thread.  It never creates a remote thread and never sends input.
DWORD WINAPI start_on_managed_thread(void* module);

}  // namespace gkms::runtime_exam_recorder

extern "C" {

__declspec(dllexport) DWORD WINAPI GKMSRuntimeExamRecorderStartOnManagedThread(
    void* module);
__declspec(dllexport) unsigned int GKMSRuntimeExamRecorderProtocolVersion();


}
