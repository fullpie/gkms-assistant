#pragma once
#include <cstdint>
#include <type_traits>
namespace gkms::bridge {
// Current Unity 6 uses an opaque pointer-sized Il2CppGCHandle, not the legacy
// uint32 slot index. See current Localify Hook.cpp / HookTexture.cpp and the
// crash ABI evidence in this target's README.
using GCHandle=void*;
using GCHandleNew=GCHandle (*)(void*,bool);
using GCHandleGetTarget=void* (*)(GCHandle);
using GCHandleFree=void (*)(GCHandle);
static_assert(sizeof(GCHandle)==sizeof(std::uintptr_t));
static_assert(sizeof(GCHandle)==8,"This target requires the current PC x64 ABI");
static_assert(std::is_pointer_v<GCHandle>);
}
