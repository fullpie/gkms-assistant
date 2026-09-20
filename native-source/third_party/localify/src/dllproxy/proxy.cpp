#include <stdinclude.hpp>
#include <cpprest/details/web_utilities.h>

extern "C"
{
	void* GetFileVersionInfoA_Original = NULL;
	void* GetFileVersionInfoByHandle_Original = NULL;
	void* GetFileVersionInfoExA_Original = NULL;
	void* GetFileVersionInfoExW_Original = NULL;
	void* GetFileVersionInfoSizeA_Original = NULL;
	void* GetFileVersionInfoSizeExA_Original = NULL;
	void* GetFileVersionInfoSizeExW_Original = NULL;
	void* GetFileVersionInfoSizeW_Original = NULL;
	void* GetFileVersionInfoW_Original = NULL;
	void* VerFindFileA_Original = NULL;
	void* VerFindFileW_Original = NULL;
	void* VerInstallFileA_Original = NULL;
	void* VerInstallFileW_Original = NULL;
	void* VerLanguageNameA_Original = NULL;
	void* VerLanguageNameW_Original = NULL;
	void* VerQueryValueA_Original = NULL;
	void* VerQueryValueW_Original = NULL;

#define RESOLVE_DEF(name)\
	void* name##_Original = NULL;

	RESOLVE_DEF(ApplyCompatResolutionQuirking);
	RESOLVE_DEF(CompatString);
	RESOLVE_DEF(CompatValue);
	RESOLVE_DEF(DXGIDumpJournal);
	RESOLVE_DEF(PIXBeginCapture);
	RESOLVE_DEF(PIXEndCapture);
	RESOLVE_DEF(PIXGetCaptureState);
	RESOLVE_DEF(SetAppCompatStringPointer);
	RESOLVE_DEF(UpdateHMDEmulationStatus);
	RESOLVE_DEF(CreateDXGIFactory);
	RESOLVE_DEF(CreateDXGIFactory1);
	RESOLVE_DEF(CreateDXGIFactory2);
	RESOLVE_DEF(DXGID3D10CreateDevice);
	RESOLVE_DEF(DXGID3D10CreateLayeredDevice);
	RESOLVE_DEF(DXGID3D10GetLayeredDeviceSize);
	RESOLVE_DEF(DXGID3D10RegisterLayers);
	RESOLVE_DEF(DXGIDeclareAdapterRemovalSupport);
	RESOLVE_DEF(DXGIGetDebugInterface1);
	RESOLVE_DEF(DXGIReportAdapterConfiguration);
}

using namespace std;

namespace
{
	class version_init
	{
	public:
		version_init()
		{
			std::string dll_path;
			dll_path.resize(MAX_PATH);
			dll_path.resize(GetSystemDirectoryA(dll_path.data(), MAX_PATH));

			dll_path += "\\" + "version.dll"s;

			auto original_dll = LoadLibraryA(dll_path.data());

			GetFileVersionInfoA_Original = GetProcAddress(original_dll, "GetFileVersionInfoA");
			GetFileVersionInfoByHandle_Original = GetProcAddress(original_dll, "GetFileVersionInfoByHandle");
			GetFileVersionInfoExA_Original = GetProcAddress(original_dll, "GetFileVersionInfoExA");
			GetFileVersionInfoExW_Original = GetProcAddress(original_dll, "GetFileVersionInfoExW");
			GetFileVersionInfoSizeA_Original = GetProcAddress(original_dll, "GetFileVersionInfoSizeA");
			GetFileVersionInfoSizeExA_Original = GetProcAddress(original_dll, "GetFileVersionInfoSizeExA");
			GetFileVersionInfoSizeExW_Original = GetProcAddress(original_dll, "GetFileVersionInfoSizeExW");
			GetFileVersionInfoSizeW_Original = GetProcAddress(original_dll, "GetFileVersionInfoSizeW");
			GetFileVersionInfoW_Original = GetProcAddress(original_dll, "GetFileVersionInfoW");
			VerFindFileA_Original = GetProcAddress(original_dll, "VerFindFileA");
			VerFindFileW_Original = GetProcAddress(original_dll, "VerFindFileW");
			VerInstallFileA_Original = GetProcAddress(original_dll, "VerInstallFileA");
			VerInstallFileW_Original = GetProcAddress(original_dll, "VerInstallFileW");
			VerLanguageNameA_Original = GetProcAddress(original_dll, "VerLanguageNameA");
			VerLanguageNameW_Original = GetProcAddress(original_dll, "VerLanguageNameW");
			VerQueryValueA_Original = GetProcAddress(original_dll, "VerQueryValueA");
			VerQueryValueW_Original = GetProcAddress(original_dll, "VerQueryValueW");
		};
	};

#define RESOLVE_ADDR(name)\
	name##_Original = GetProcAddress(original_dll, #name)

	class dxgi_init {
	public :
		dxgi_init() {
			std::string dll_path;
			dll_path.resize(MAX_PATH);
			dll_path.resize(GetSystemDirectoryA(dll_path.data(), MAX_PATH));

			dll_path += "\\" + "dxgi.dll"s;

			auto original_dll = LoadLibraryA(dll_path.data());

			RESOLVE_ADDR(ApplyCompatResolutionQuirking);
			RESOLVE_ADDR(CompatString);
			RESOLVE_ADDR(CompatValue);
			RESOLVE_ADDR(DXGIDumpJournal);
			RESOLVE_ADDR(PIXBeginCapture);
			RESOLVE_ADDR(PIXEndCapture);
			RESOLVE_ADDR(PIXGetCaptureState);
			RESOLVE_ADDR(SetAppCompatStringPointer);
			RESOLVE_ADDR(UpdateHMDEmulationStatus);
			RESOLVE_ADDR(CreateDXGIFactory);
			RESOLVE_ADDR(CreateDXGIFactory1);
			RESOLVE_ADDR(CreateDXGIFactory2);
			RESOLVE_ADDR(DXGID3D10CreateDevice);
			RESOLVE_ADDR(DXGID3D10CreateLayeredDevice);
			RESOLVE_ADDR(DXGID3D10GetLayeredDeviceSize);
			RESOLVE_ADDR(DXGID3D10RegisterLayers);
			RESOLVE_ADDR(DXGIDeclareAdapterRemovalSupport);
			RESOLVE_ADDR(DXGIGetDebugInterface1);
			RESOLVE_ADDR(DXGIReportAdapterConfiguration);
		}
	};

	version_init init {};
	dxgi_init dx_init{};
}
