#include "windowsPlatform.hpp"
#include "GakumasLocalify/Plugin.h"
#include "GakumasLocalify/Log.h"
#include "GakumasLocalify/Local.h"
#include "GakumasLocalify/MasterLocal.h"
#include "GakumasLocalify/camera/camera.hpp"
#include "GakumasLocalify/config/Config.hpp"
#include "GakumasLocalify/Il2cppUtils.hpp"
#include "gkmsGUI/gkmsGUIMain.hpp"
#include "gkmsGUI/GUII18n.hpp"
#include "resourceUpdate/resourceUpdate.hpp"

#include <stdinclude.hpp>
#include "deps/UnityResolve/UnityResolve.hpp"
#include <cpprest/details/basic_types.h>
#include <cpprest/details/http_helpers.h>
#include <windowsx.h>

bool mh_inited;
extern std::filesystem::path gakumasLocalPath;
extern std::filesystem::path ConfigJson;
extern std::filesystem::path ProgramConfigJson;
int hotk = 'u';

void reload_all_data();

std::function<void()> g_on_close;
std::function<void()> on_hotKey_0;
std::function<void()> g_reload_all_data = reload_all_data;

void patchGameAssenbly();
void readProgramConfig();

namespace
{
    class AndroidHookInstaller : public GakumasLocal::HookInstaller
    {
    public:
        explicit AndroidHookInstaller(const std::string& il2cppLibraryPath, const std::string& localizationFilesDir)
        {
            this->m_Il2CppLibrary = GetModuleHandle("GameAssembly.dll");
            this->m_il2cppLibraryPath = il2cppLibraryPath;
            this->localizationFilesDir = localizationFilesDir;
        }

        ~AndroidHookInstaller() override {
			if (!mh_inited) return;
            MH_DisableHook(MH_ALL_HOOKS);
            MH_Uninitialize();
        }

        void* InstallHook(void* addr, void* hook, void** orig) override
        {
            auto createStat = MH_CreateHook(addr, hook, orig);
            MH_EnableHook(addr);
            return reinterpret_cast<void*>(createStat);
        }

        GakumasLocal::OpaqueFunctionPointer LookupSymbol(const char* name) override
        {
            return reinterpret_cast<GakumasLocal::OpaqueFunctionPointer>(GetProcAddress(reinterpret_cast<HMODULE>(m_Il2CppLibrary), name));
        }

    private:
        void* m_Il2CppLibrary;
    };
}

void* load_library_w_orig;
HMODULE __stdcall load_library_w_hook(const wchar_t* path)
{
    // GakumasLocal::Log::DebugFmt("LoadLibrary %s", utility::conversions::to_utf8string(path).c_str());

    using namespace std;
    if (path == L"VuplexWebViewWindows.dll"sv) {
    // if (path == L"cri_ware_unity.dll"sv) {
    // if (path == L"NVUnityPlugin"sv) {
        patchGameAssenbly();
    }
    return reinterpret_cast<decltype(LoadLibraryW)*>(load_library_w_orig)(path);
}

void patchGameAssenbly() {
	static bool patched = false;
	if (patched) return;
	patched = true;

    auto& plugin = GakumasLocal::Plugin::GetInstance();
    plugin.InstallHook(std::make_unique<AndroidHookInstaller>("GameAssembly.dll", gakumasLocalPath.string()));
}

void unInitHook() {
    if (!mh_inited) return;
    MH_DisableHook(MH_ALL_HOOKS);
    MH_Uninitialize();
}

void initHook() {
	mh_inited = true;

    if (MH_Initialize() != MH_OK)
        return;

    auto stat1 = MH_CreateHook(LoadLibraryW, load_library_w_hook, &load_library_w_orig);
    auto stat2 = MH_EnableHook(LoadLibraryW);

    g_on_close = []() {
        unInitHook();
        TerminateProcess(GetCurrentProcess(), 0);
        };

    static bool guiStarting = false;
	on_hotKey_0 = []() {
        if (guiStarting) return;
        guiStarting = true;
        std::thread([]() {
            printf("GUI START\n");
            guimain();
            guiStarting = false;
            printf("GUI END\n");
            }).detach();
		};

    GakumasLocal::Log::InfoFmt("initHook stat: %s - %s\n", MH_StatusToString(stat1), MH_StatusToString(stat2));
}

void checkAndInitConfig(const std::filesystem::path& LocalConfigFile) {
    if (!std::filesystem::exists(ProgramConfigJson)) {
        g_useAPIAssetsURL = GkmsGUII18n::ts("default_assets_check_api");
        GkmsResourceUpdate::saveProgramConfig();
    }
    if (!std::filesystem::exists(LocalConfigFile)) {
        GakumasLocal::Config::SaveConfig(LocalConfigFile.string());
    }
}


void loadConfig(const std::string& configJson) {
	GakumasLocal::Config::LoadConfig(configJson);
}

void loadConfig(const std::filesystem::path& filePath) {
	checkAndInitConfig(filePath);

    std::ifstream file(filePath);
    if (!file.is_open()) {
        GakumasLocal::Log::ErrorFmt("Load config %s failed.\n", filePath.string().c_str());
        loadConfig(std::string("{}"));
        return;
    }
    std::string fileContent((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    file.close();

    loadConfig(fileContent);
}

void reload_all_data() {
    readProgramConfig();
	loadConfig(ConfigJson);
    GkmsResourceUpdate::GetCurrentResourceVersion(false);
    GkmsResourceUpdate::GetCurrentTextureVersion(false);
	GakumasLocal::Local::LoadData();
	GakumasLocal::MasterLocal::LoadData();
}

bool getCurrentLodingProgress(int* stepTotal, int* stepCurrent, int* currTotal, int* currCurrent) {
    *stepTotal = UnityResolveProgress::assembliesProgress.total;
	*stepCurrent = UnityResolveProgress::assembliesProgress.current;
	*currTotal = UnityResolveProgress::classProgress.total;
	*currCurrent = UnityResolveProgress::classProgress.current;

    return UnityResolveProgress::startInit;
}

void checkDBGKey(int action, int key_code) {
	if (action != WM_KEYDOWN) return;
	static const std::vector<int> targetDbgKeyList = { 38, 38, 40, 40, 37, 39, 37, 39, 66, 65};
    static int currentIndex = 0;
    if (targetDbgKeyList[currentIndex] == key_code) {
        if (currentIndex == targetDbgKeyList.size() - 1) {
            currentIndex = 0;
			GakumasLocal::Config::dbgMode = !GakumasLocal::Config::dbgMode;
			GakumasLocal::Config::SaveConfig(ConfigJson.string());
            GakumasLocal::Log::InfoFmt("Debug Mode: %d", GakumasLocal::Config::dbgMode);
        }
        else {
            currentIndex++;
        }
    }
    else {
        currentIndex = 0;
    }
}

void keyboardEvents(int action, int key_code) {
	// GakumasLocal::Log::DebugFmt("keyboardEvents: %d - %d", action, key_code);
	checkDBGKey(action, key_code);
    GKCamera::on_cam_rawinput_keyboard(action, key_code);
    const auto msg = GakumasLocal::Local::OnKeyDown(action, key_code);
    if (!msg.empty()) {
		GakumasLocal::Log::Info(msg.c_str());
    }
}

namespace GakumasLocal::WinHooks {
    using Il2cppString = UnityResolve::UnityType::String;

	void* LoadAssetBundle(const std::string& path) {
        std::filesystem::path abs_path = std::filesystem::absolute(path).lexically_normal();
        Il2cppString* bundlePath = Il2cppString::New(abs_path.wstring());

        static auto LoadFromFileAsync = Il2cppUtils::GetMethod("UnityEngine.AssetBundleModule.dll", "UnityEngine", "AssetBundle", "LoadFromFileAsync");
        static auto get_assetBundle = Il2cppUtils::GetMethod("UnityEngine.AssetBundleModule.dll", "UnityEngine", "AssetBundleCreateRequest", "get_assetBundle");

        auto bundleTask = LoadFromFileAsync->Invoke<void*>(bundlePath);

		if (!bundleTask) {
			return nullptr;
		}
		return get_assetBundle->Invoke<void*>(bundleTask);
	}

    void SwitchFullScreen(HWND hWnd) {
        static auto Screen_SetResolution = reinterpret_cast<void (*)(UINT, UINT, UINT, void*)>(
            Il2cppUtils::il2cpp_resolve_icall("UnityEngine.Screen::SetResolution_Injected(System.Int32,System.Int32,UnityEngine.FullScreenMode,UnityEngine.RefreshRate&)"));
        static auto get_Height = reinterpret_cast<int (*)()>(Il2cppUtils::il2cpp_resolve_icall("UnityEngine.Screen::get_height()"));
        static auto get_Width = reinterpret_cast<int (*)()>(Il2cppUtils::il2cpp_resolve_icall("UnityEngine.Screen::get_width()"));

        LONG style = GetWindowLong(hWnd, GWL_STYLE);
        bool currFullScreen = style & WS_POPUP;

		static int savedWidth = -1;
		static int savedHeight = -1;

        int64_t v8[3];
        v8[0] = 0x100000000LL;

        if (currFullScreen) {
            // 取消全屏
            if (savedWidth == -1) {
                savedWidth = 542;
                savedHeight = 990;
            }
            Screen_SetResolution(savedWidth, savedHeight, 2 * !false + 1, v8);
        }
        else {
			savedWidth = get_Width();
			savedHeight = get_Height();
            Screen_SetResolution(GetSystemMetrics(SM_CXSCREEN), GetSystemMetrics(SM_CYSCREEN), 2 * !true + 1, v8);
        }
    }

    namespace Keyboard {
        std::function<void(int, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD)> mKeyBoardCallBack = nullptr;

        WNDPROC g_pfnOldWndProc = NULL;
        LRESULT CALLBACK WndProcCallback(HWND hWnd, UINT uMsg, WPARAM wParam, LPARAM lParam) {
            DWORD SHIFT_key = 0;
            DWORD CTRL_key = 0;
            DWORD ALT_key = 0;
            DWORD SPACE_key = 0;
            DWORD UP_key = 0;
            DWORD DOWN_key = 0;
            DWORD LEFT_key = 0;
            DWORD RIGHT_key = 0;

			// printf("WndProcCallback: 0x%x (%d)\n", uMsg, uMsg);

            switch (uMsg) {
            case WM_INPUT: {
                RAWINPUT rawInput;
                UINT size = sizeof(RAWINPUT);
                if (GetRawInputData((HRAWINPUT)lParam, RID_INPUT, &rawInput, &size, sizeof(RAWINPUTHEADER)) == size) {

                    /* 鼠标事件，后面加上
                    if (rawInput.header.dwType == RIM_TYPEMOUSE)
                    {
                        switch (rawInput.data.mouse.ulButtons) {
                        case 0: {  // move
                            SCCamera::mouseMove(rawInput.data.mouse.lLastX, rawInput.data.mouse.lLastY, 3);
                        }; break;
                        case 4: {  // press
                            SCCamera::mouseMove(0, 0, 1);
                        }; break;
                        case 8: {  // release
                            SCCamera::mouseMove(0, 0, 2);
                        }; break;
                        default: break;
                        }

                        if (rawInput.data.mouse.usButtonFlags == RI_MOUSE_WHEEL) {
                            if (rawInput.data.mouse.usButtonData == 120) {
                                SCCamera::mouseMove(0, 1, 4);
                            }
                            else {
                                SCCamera::mouseMove(0, -1, 4);
                            }
                        }

                    }*/
                }
            }; break;

            case WM_SYSKEYUP:
            case WM_KEYUP: {
                int key = wParam;
                keyboardEvents(uMsg, key);
            }; break;

            case WM_SYSKEYDOWN:
            case WM_KEYDOWN: {
                int key = wParam;

                keyboardEvents(uMsg, key);
                SHIFT_key = GetAsyncKeyState(VK_SHIFT);
                CTRL_key = GetAsyncKeyState(VK_CONTROL);
                ALT_key = GetAsyncKeyState(VK_MENU);
                SPACE_key = GetAsyncKeyState(VK_SPACE);

                UP_key = GetAsyncKeyState(VK_UP);
                DOWN_key = GetAsyncKeyState(VK_DOWN);
                LEFT_key = GetAsyncKeyState(VK_LEFT);
                RIGHT_key = GetAsyncKeyState(VK_RIGHT);

                if (mKeyBoardCallBack != nullptr) {
                    mKeyBoardCallBack(key, SHIFT_key, CTRL_key, ALT_key, SPACE_key, UP_key, DOWN_key, LEFT_key, RIGHT_key);
                }
				
                if (key == 122) {
					// F11
                    if (GakumasLocal::Config::dmmUnlockSize) {
                        SwitchFullScreen(hWnd);
                    }
                }
                if (key >= 'A' && key <= 'Z')
                {

                    if (GetAsyncKeyState(VK_SHIFT) >= 0) key += 32;

                    if (CTRL_key != 0 && key == hotk)
                    {
                        // fopenExternalPlugin(tlgport);
                        printf("hotKey pressed.\n");
                        if (on_hotKey_0) on_hotKey_0();
                    }

                    SHIFT_key = 0;
                    CTRL_key = 0;
                    ALT_key = 0;
                    SPACE_key = 0;
                    DWORD UP_key = 0;
                    DWORD DOWN_key = 0;
                    DWORD LEFT_key = 0;
                    DWORD RIGHT_key = 0;
                }
            }; break;
            case WM_NCACTIVATE: {
                if (!wParam) {
                    // SCCamera::onKillFocus();
                }
            }; break;
            case WM_KILLFOCUS: {
                // SCCamera::onKillFocus();
            }; break;
            case WM_CLOSE: {
                if (g_on_close) g_on_close();
            }; break;

            case WM_NCHITTEST:
            {
                if (GakumasLocal::Config::dmmUnlockSize) {
                    POINT pt = { GET_X_LPARAM(lParam), GET_Y_LPARAM(lParam) };
                    ScreenToClient(hWnd, &pt);
                    RECT rcClient;
                    GetClientRect(hWnd, &rcClient);
                    const int borderWidth = 8; // 根据需要调整边缘宽度

                    bool left = pt.x < borderWidth;
                    bool right = pt.x >= rcClient.right - borderWidth;
                    bool top = pt.y < borderWidth;
                    bool bottom = pt.y >= rcClient.bottom - borderWidth;

                    if (top && left) return HTTOPLEFT;
                    if (top && right) return HTTOPRIGHT;
                    if (bottom && left) return HTBOTTOMLEFT;
                    if (bottom && right) return HTBOTTOMRIGHT;
                    if (left) return HTLEFT;
                    if (right) return HTRIGHT;
                    if (top) return HTTOP;
                    if (bottom) return HTBOTTOM;
                    return HTCLIENT;
                }
            } break;

            case WM_GETMINMAXINFO:
            {
                if (GakumasLocal::Config::dmmUnlockSize) {
                    LPMINMAXINFO lpMMI = (LPMINMAXINFO)lParam;
                    // 设置最大尺寸为屏幕分辨率，这样就不限制窗口的最大尺寸
                    lpMMI->ptMaxTrackSize.x = GetSystemMetrics(SM_CXSCREEN) * 3;
                    lpMMI->ptMaxTrackSize.y = GetSystemMetrics(SM_CYSCREEN) * 3;
                    // 可选：设置窗口最小尺寸（例如200x200）
                    lpMMI->ptMinTrackSize.x = 200;
                    lpMMI->ptMinTrackSize.y = 200;
                    return 1;
                }
            } break;

            case WM_SYSCOMMAND: {
                if (GakumasLocal::Config::dmmUnlockSize) {
                    if ((wParam & 0xFFF0) == SC_MAXIMIZE) {
                        SwitchFullScreen(hWnd);
                        return 1;
                    }
                }
            } break;

			case WM_NCPAINT: {
                if (GakumasLocal::Config::dmmUnlockSize) {
                    LONG style = GetWindowLong(hWnd, GWL_STYLE);
                    // printf("WM_NCPAINT: 0x%x\n", style);

                    if (!(style & WS_POPUP)) {
                        // 添加可调整大小的边框和最大化按钮
                        style |= WS_THICKFRAME | WS_MAXIMIZEBOX;
                        SetWindowLong(hWnd, GWL_STYLE, style);
                    }
                }

			} break;

            default: break;
            }

            return CallWindowProc(g_pfnOldWndProc, hWnd, uMsg, wParam, lParam);
        }

        void InstallWndProcHook() {
            auto hWnd = FindWindowW(L"UnityWndClass", L"gakumas");
            g_pfnOldWndProc = (WNDPROC)GetWindowLongPtr(hWnd, GWLP_WNDPROC);
            SetWindowLongPtr(FindWindowW(L"UnityWndClass", L"gakumas"), GWLP_WNDPROC, (LONG_PTR)WndProcCallback);
            // 添加可调整大小的边框和最大化按钮
            LONG style = GetWindowLong(hWnd, GWL_STYLE);
            style |= WS_THICKFRAME | WS_MAXIMIZEBOX;
            SetWindowLong(hWnd, GWL_STYLE, style);
        }

        void UninstallWndProcHook(HWND hWnd)
        {
            SetWindowLongPtr(hWnd, GWLP_WNDPROC, (LONG_PTR)g_pfnOldWndProc);
        }

    }

}
