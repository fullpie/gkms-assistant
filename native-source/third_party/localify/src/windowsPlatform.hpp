#pragma once
#include <string>
#include <filesystem>

void initHook();

void unInitHook();

void loadConfig(const std::string& configJson);

void loadConfig(const std::filesystem::path& filePath);

bool getCurrentLodingProgress(int* stepTotal, int* stepCurrent, int* currTotal, int* currCurrent);

// TODO keyboard events

namespace GakumasLocal::WinHooks {
	void* LoadAssetBundle(const std::string& path);

	namespace Keyboard {
		void InstallWndProcHook();
	}
}