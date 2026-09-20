workspace "gkms_public_control"
    location "build/projects"
    architecture "x86_64"
    configurations { "Release" }
    system "windows"
    systemversion "latest"
project "gkms_runtime_command_bridge"
    kind "SharedLib"
    language "C++"
    cppdialect "C++20"
    characterset "Unicode"
    staticruntime "Off"
    optimize "Speed"
    symbols "On"
    targetdir "build/bin"
    objdir "build/obj/gkms_runtime_command_bridge"
    defines { "GKMS_PUBLIC_PORTABLE", "WIN32_LEAN_AND_MEAN", "NOMINMAX" }
    includedirs { "native/runtime_command_bridge/src", "native/runtime_exam_recorder/src", "native/telemetry_observer/src", "native/shared", "third_party/localify/src/deps", "third_party/localify/deps/minhook/include", "third_party/localify/deps/minhook/src" }
    links { "Bcrypt", "Psapi" }
    buildoptions { "/experimental:deterministic", '/pathmap:"' .. path.getabsolute(".") .. '=public-source"' }
    linkoptions { "/PDBALTPATH:gkms_runtime_command_bridge.pdb" }
    files { "native/runtime_command_bridge/src/audition_result_adapter.cpp", "native/runtime_command_bridge/src/audition_retry_adapter.cpp", "native/runtime_command_bridge/src/bridge.cpp", "native/runtime_command_bridge/src/callback_method_contract.cpp", "native/runtime_command_bridge/src/continuation.cpp", "native/runtime_command_bridge/src/customize_confirmation_adapter.cpp", "native/runtime_command_bridge/src/drink_inventory_adapter.cpp", "native/runtime_command_bridge/src/error_adapter.cpp", "native/runtime_command_bridge/src/exam_adapter.cpp", "native/runtime_command_bridge/src/exam_card_phase_observation.cpp", "native/runtime_command_bridge/src/exam_model_observation.cpp", "native/runtime_command_bridge/src/exam_reference_presence.cpp", "native/runtime_command_bridge/src/exam_save_serializer.cpp", "native/runtime_command_bridge/src/interval_adapter.cpp", "native/runtime_command_bridge/src/inventory_adapter.cpp", "native/runtime_command_bridge/src/live_loading_adapter.cpp", "native/runtime_command_bridge/src/live_presentation_adapter.cpp", "native/runtime_command_bridge/src/mailbox.cpp", "native/runtime_command_bridge/src/memory_auto_adapter.cpp", "native/runtime_command_bridge/src/mode_navigation_adapter.cpp", "native/runtime_command_bridge/src/native_trace.cpp", "native/runtime_command_bridge/src/outer_adapter.cpp", "native/runtime_command_bridge/src/outer_pointer_guard.cpp", "native/runtime_command_bridge/src/pc_method_binding.cpp", "native/runtime_command_bridge/src/pc_version_profile.cpp", "native/runtime_command_bridge/src/produce_card_ui_adapter.cpp", "native/runtime_command_bridge/src/produce_effect_view_adapter.cpp", "native/runtime_command_bridge/src/produce_lifecycle_adapter.cpp", "native/runtime_command_bridge/src/produce_result_adapter.cpp", "native/runtime_command_bridge/src/recommended_replay_adapter.cpp", "native/runtime_command_bridge/src/replay_entry_adapter.cpp", "native/runtime_command_bridge/src/result_score_notice_adapter.cpp", "native/runtime_command_bridge/src/reward_drink_capacity_adapter.cpp", "native/runtime_command_bridge/src/runtime.cpp", "native/runtime_command_bridge/src/screen_context.cpp", "native/runtime_command_bridge/src/selection_memory_adapter.cpp", "native/runtime_command_bridge/src/shop_adapter.cpp", "native/runtime_command_bridge/src/story_adapter.cpp" }
project "gkms_runtime_exam_recorder"
    kind "SharedLib"
    language "C++"
    cppdialect "C++20"
    characterset "Unicode"
    staticruntime "Off"
    optimize "Speed"
    symbols "On"
    targetdir "build/bin"
    objdir "build/obj/gkms_runtime_exam_recorder"
    defines { "GKMS_PUBLIC_PORTABLE", "WIN32_LEAN_AND_MEAN", "NOMINMAX" }
    includedirs { "native/runtime_command_bridge/src", "native/runtime_exam_recorder/src", "native/telemetry_observer/src", "native/shared", "third_party/localify/src/deps", "third_party/localify/deps/minhook/include", "third_party/localify/deps/minhook/src" }
    links { "Bcrypt", "Psapi" }
    buildoptions { "/experimental:deterministic", '/pathmap:"' .. path.getabsolute(".") .. '=public-source"' }
    linkoptions { "/PDBALTPATH:gkms_runtime_exam_recorder.pdb" }
    files { "third_party/localify/deps/minhook/src/buffer.c", "third_party/localify/deps/minhook/src/hde/hde32.c", "third_party/localify/deps/minhook/src/hde/hde64.c", "third_party/localify/deps/minhook/src/hook.c", "third_party/localify/deps/minhook/src/trampoline.c", "native/runtime_command_bridge/src/pc_method_binding.cpp", "native/runtime_command_bridge/src/pc_version_profile.cpp", "native/telemetry_observer/src/il2cpp_api.cpp", "native/runtime_exam_recorder/src/dllmain.cpp", "native/runtime_exam_recorder/src/recorder.cpp" }
