#include <stdinclude.hpp>

#include <minizip/unzip.h>
#include <TlHelp32.h>

#include <unordered_set>
#include <charconv>
#include <cassert>
#include <format>
#include <cpprest/uri.h>
#include <cpprest/http_listener.h>
#include <ranges>
#include <windows.h>
#include "windowsPlatform.hpp"

extern void start_console();

const auto CONSOLE_TITLE = L"Gakumas Localify";

std::filesystem::path gakumasLocalPath = "./gakumas-local";
std::filesystem::path ProgramConfigJson = gakumasLocalPath / "config.json";
std::filesystem::path ConfigJson = gakumasLocalPath / "localizationConfig.json";

bool g_has_config_file = false;
#if defined(GKMS_PUBLIC_PORTABLE)
bool g_enable_console = false;
#else
bool g_enable_console = true;
#endif
bool g_useRemoteAssets = false;
bool g_useAPIAssets = false;
bool g_useAPITextureAssets = false;
bool g_delTextureRemoteAfterUpdate = true;
std::string g_remoteResourceUrl = "";
std::string g_useAPIAssetsURL = "";
std::string g_useAPITextureAssetsURL = "https://texture.gakumas.cn/api/gkms_texture_data";

namespace
{
	void create_debug_console()
	{
		AllocConsole();
#if defined(GKMS_PUBLIC_PORTABLE)
        // A selection must not suspend the game's stdout/managed callback.
        const auto input = GetStdHandle(STD_INPUT_HANDLE);
        DWORD mode{};
        if (input != INVALID_HANDLE_VALUE && input != nullptr && GetConsoleMode(input, &mode))
            SetConsoleMode(input, (mode | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE);
#endif

		// open stdout stream
		auto _ = freopen("CONOUT$", "w+t", stdout);
		_ = freopen("CONOUT$", "w", stderr);
		_ = freopen("CONIN$", "r", stdin);

		SetConsoleTitleW(CONSOLE_TITLE);

		// set this to avoid turn japanese texts into question mark
		SetConsoleOutputCP(65001);
		std::locale::global(std::locale(""));

		wprintf(L"%ls Loaded! - By chinosk\n", CONSOLE_TITLE);
	}
}

void readProgramConfig() {
	std::vector<std::string> dicts{};
	std::ifstream config_stream{ ProgramConfigJson };

	if (!config_stream.is_open())
		return;

	rapidjson::IStreamWrapper wrapper{ config_stream };
	rapidjson::Document document;

	document.ParseStream(wrapper);

	if (!document.HasParseError())
	{
		g_has_config_file = true;
		if (document.HasMember("enableConsole")) {
			g_enable_console = document["enableConsole"].GetBool();
		}

		if (document.HasMember("useRemoteAssets")) {
			g_useRemoteAssets = document["useRemoteAssets"].GetBool();
		}

		if (document.HasMember("transRemoteZipUrl")) {
			g_remoteResourceUrl = document["transRemoteZipUrl"].GetString();
		}

		if (document.HasMember("useAPIAssets")) {
			g_useAPIAssets = document["useAPIAssets"].GetBool();
		}

		if (document.HasMember("useAPIAssetsURL")) {
			g_useAPIAssetsURL = document["useAPIAssetsURL"].GetString();
		}

		if (document.HasMember("useAPITextureAssets")) {
			g_useAPITextureAssets = document["useAPITextureAssets"].GetBool();
		}

		if (document.HasMember("useAPITextureAssetsURL")) {
			g_useAPITextureAssetsURL = document["useAPITextureAssetsURL"].GetString();
		}

		if (document.HasMember("delTextureRemoteAfterUpdate")) {
			g_delTextureRemoteAfterUpdate = document["delTextureRemoteAfterUpdate"].GetBool();
		}

	}
	config_stream.close();
}

LONG WINAPI AddressExceptionHandler(EXCEPTION_POINTERS* ExceptionInfo) {
	// Check if the exception is an access violation
	if (ExceptionInfo->ExceptionRecord->ExceptionCode == EXCEPTION_ACCESS_VIOLATION) {
		// Check if the violation address is above 0x7FFFFFFFFFFF
		if (ExceptionInfo->ExceptionRecord->ExceptionAddress > (PVOID)0x7FFFFFFFFFFF) {
			// Return EXCEPTION_CONTINUE_EXECUTION to ignore the exception and continue
			return EXCEPTION_CONTINUE_EXECUTION;
		}
	}
	// For other exceptions or addresses, use the default exception handler
	return EXCEPTION_CONTINUE_SEARCH;
}


int __stdcall DllMain(HINSTANCE dllModule, DWORD reason, LPVOID)
{
	if (reason == DLL_PROCESS_ATTACH)
	{
		AddVectoredExceptionHandler(1, AddressExceptionHandler);
		// the DMM Launcher set start path to system32 wtf????
		std::string module_name;
		module_name.resize(MAX_PATH);
		module_name.resize(GetModuleFileName(nullptr, module_name.data(), MAX_PATH));

		std::filesystem::path module_path(module_name);

		// check name
		if (module_path.filename() != "gakumas.exe")
			return 1;

		std::filesystem::current_path(
			module_path.parent_path()
		);

		readProgramConfig();

		if (g_enable_console)
			create_debug_console();

		std::thread init_thread([] {

			if (g_enable_console)
			{
				start_console();
#if !defined(GKMS_PUBLIC_PORTABLE)
                printf("Command: %s\n", GetCommandLineA());
#endif
			}

			loadConfig(ConfigJson);
			initHook();

			std::mutex mutex;
			std::condition_variable cond;
			std::atomic<bool> hookIsReady(false);

			// 渚濊禆妫€鏌ユ父鎴忕増鏈殑鎸囬拡鍔犺浇锛屽洜姝ゅ湪 hook 瀹屾垚鍚庡啀鍔犺浇缈昏瘧鏁版嵁
			std::unique_lock lock(mutex);
			cond.wait(lock, [&] {
				return hookIsReady.load(std::memory_order_acquire);
				});
			if (g_enable_console)
			{
				auto _ = freopen("CONOUT$", "w+t", stdout);
				_ = freopen("CONOUT$", "w", stderr);
				_ = freopen("CONIN$", "r", stdin);
			}

			});
		init_thread.detach();
	}
	else if (reason == DLL_PROCESS_DETACH)
	{
		RemoveVectoredExceptionHandler(AddressExceptionHandler);
		unInitHook();
	}
	return 1;
}
