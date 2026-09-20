#pragma once

#include <Windows.h>
#include <filesystem>
#include <stdexcept>
#include <string>

#if defined(GKMS_PUBLIC_PORTABLE) && (defined(GKMS_RUNTIME_COMMAND_TRACE) || defined(GKMS_DIRECT_REPLAY_RESEARCH) || \
    defined(GKMS_OFFICIAL_REPLAY_CORE) || defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || \
    defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE) || defined(GKMS_RUNTIME_HAND_VALIDATOR_PROBE) || \
    defined(GKMS_RUNTIME_VALIDATOR_ABI_PROBE) || defined(GKMS_RUNTIME_PASSIVE_POLICY_PROBE))
#error Public portable builds cannot include research adapters or optional probes.
#endif

namespace gkms::public_runtime_paths {

inline std::filesystem::path state_root() {
    const DWORD required = GetEnvironmentVariableW(L"LOCALAPPDATA", nullptr, 0);
    if (required <= 1 || required > 32768)
        throw std::runtime_error("LOCALAPPDATA is unavailable for the public runtime");
    std::wstring buffer(required, L'\0');
    const DWORD length = GetEnvironmentVariableW(L"LOCALAPPDATA", buffer.data(), required);
    if (length == 0 || length >= required)
        throw std::runtime_error("LOCALAPPDATA changed while resolving the public runtime path");
    buffer.resize(length);
    const std::filesystem::path base(buffer);
    if (!base.is_absolute())
        throw std::runtime_error("LOCALAPPDATA must be an absolute path");
    return base / L"gkms-assistant";
}

inline std::filesystem::path native_root() {
    std::wstring buffer(32768, L'\0');
    const DWORD length = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    if (length == 0 || length >= buffer.size())
        throw std::runtime_error("Game executable path is unavailable for the public runtime");
    buffer.resize(length);
    const std::filesystem::path executable(buffer);
    if (!executable.is_absolute() || executable.parent_path().empty())
        throw std::runtime_error("Game executable path must be absolute");
    return executable.parent_path() / L"gkms" / L"native";
}

} // namespace gkms::public_runtime_paths
