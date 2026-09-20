#pragma once
#include <minhook.h>
#include <vector>

#define ANDROID_LOG_VERBOSE 0
#define ANDROID_LOG_DEBUG 1
#define ANDROID_LOG_INFO 2
#define ANDROID_LOG_ERROR 3

#define GKMS_WINDOWS

#define LogMinVersion ANDROID_LOG_DEBUG

#define PLUGIN_VERSION "v3.4.1"

#define ADD_HOOK(name, addr)                                                                       \
	name##_Addr = reinterpret_cast<name##_Type>(addr);                                             \
	if (addr) {                                                                                    \
    	auto stub = hookInstaller->InstallHook(reinterpret_cast<void*>(addr),                      \
                                               reinterpret_cast<void*>(name##_Hook),               \
                                               reinterpret_cast<void**>(&name##_Orig));            \
        if (stub) {                                                                                \
            Log::ErrorFmt("ADD_HOOK: %s at %p failed: %s", #name, addr,                            \
            MH_StatusToString(static_cast<MH_STATUS>(reinterpret_cast<int>(stub))));               \
        }                                                                                          \
        else {                                                                                     \
            hookedStubs.emplace(stub);                                                             \
            GakumasLocal::Log::InfoFmt("ADD_HOOK: %s at %p", #name, addr);                         \
        }                                                                                          \
    }                                                                                              \
    else GakumasLocal::Log::ErrorFmt("Hook failed: %s is NULL", #name, addr);                      \
    if (Config::lazyInit) UnityResolveProgress::classProgress.current++


static void __android_log_write(int prio, const char* tag, const char* msg) {
    if (prio < LogMinVersion) return;

	static const char* logLevels[4] = { "VERBOSE", "DEBUG", "INFO", "ERROR" };

	if (prio < 0 || prio > 3) {
		prio = 1;
	}
	printf("[%s] %s: %s\n", logLevels[prio], tag, msg);  
}
