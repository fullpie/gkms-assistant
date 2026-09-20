.code

extern GetFileVersionInfoA_Original:QWORD
extern GetFileVersionInfoByHandle_Original:QWORD
extern GetFileVersionInfoExA_Original:QWORD
extern GetFileVersionInfoExW_Original:QWORD
extern GetFileVersionInfoSizeA_Original:QWORD
extern GetFileVersionInfoSizeExA_Original:QWORD
extern GetFileVersionInfoSizeExW_Original:QWORD
extern GetFileVersionInfoSizeW_Original:QWORD
extern GetFileVersionInfoW_Original:QWORD
extern VerFindFileA_Original:QWORD
extern VerFindFileW_Original:QWORD
extern VerInstallFileA_Original:QWORD
extern VerInstallFileW_Original:QWORD
extern VerLanguageNameA_Original:QWORD
extern VerLanguageNameW_Original:QWORD
extern VerQueryValueA_Original:QWORD
extern VerQueryValueW_Original:QWORD

extern ApplyCompatResolutionQuirking_Original:QWORD
extern CompatString_Original:QWORD
extern CompatValue_Original:QWORD
extern DXGIDumpJournal_Original:QWORD
extern PIXBeginCapture_Original:QWORD
extern PIXEndCapture_Original:QWORD
extern PIXGetCaptureState_Original:QWORD
extern SetAppCompatStringPointer_Original:QWORD
extern UpdateHMDEmulationStatus_Original:QWORD
extern CreateDXGIFactory_Original:QWORD
extern CreateDXGIFactory1_Original:QWORD
extern CreateDXGIFactory2_Original:QWORD
extern DXGID3D10CreateDevice_Original:QWORD
extern DXGID3D10CreateLayeredDevice_Original:QWORD
extern DXGID3D10GetLayeredDeviceSize_Original:QWORD
extern DXGID3D10RegisterLayers_Original:QWORD
extern DXGIDeclareAdapterRemovalSupport_Original:QWORD
extern DXGIGetDebugInterface1_Original:QWORD
extern DXGIReportAdapterConfiguration_Original:QWORD

GetFileVersionInfoA_EXPORT proc
  jmp QWORD ptr GetFileVersionInfoA_Original
GetFileVersionInfoA_EXPORT endp

GetFileVersionInfoByHandle_EXPORT proc
  jmp QWORD ptr GetFileVersionInfoByHandle_Original
GetFileVersionInfoByHandle_EXPORT endp

GetFileVersionInfoExA_EXPORT proc
  jmp QWORD ptr GetFileVersionInfoExA_Original
GetFileVersionInfoExA_EXPORT endp

GetFileVersionInfoExW_EXPORT proc
  jmp QWORD ptr GetFileVersionInfoExW_Original
GetFileVersionInfoExW_EXPORT endp

GetFileVersionInfoSizeA_EXPORT proc
  jmp QWORD ptr GetFileVersionInfoSizeA_Original
GetFileVersionInfoSizeA_EXPORT endp

GetFileVersionInfoSizeExA_EXPORT proc
  jmp QWORD ptr GetFileVersionInfoSizeExA_Original
GetFileVersionInfoSizeExA_EXPORT endp

GetFileVersionInfoSizeExW_EXPORT proc
  jmp QWORD ptr GetFileVersionInfoSizeExW_Original
GetFileVersionInfoSizeExW_EXPORT endp

GetFileVersionInfoSizeW_EXPORT proc
  jmp QWORD ptr GetFileVersionInfoSizeW_Original
GetFileVersionInfoSizeW_EXPORT endp

GetFileVersionInfoW_EXPORT proc
  jmp QWORD ptr GetFileVersionInfoW_Original
GetFileVersionInfoW_EXPORT endp

VerFindFileA_EXPORT proc
  jmp QWORD ptr VerFindFileA_Original
VerFindFileA_EXPORT endp

VerFindFileW_EXPORT proc
  jmp QWORD ptr VerFindFileW_Original
VerFindFileW_EXPORT endp

VerInstallFileA_EXPORT proc
  jmp QWORD ptr VerInstallFileA_Original
VerInstallFileA_EXPORT endp

VerInstallFileW_EXPORT proc
  jmp QWORD ptr VerInstallFileW_Original
VerInstallFileW_EXPORT endp

VerLanguageNameA_EXPORT proc
  jmp QWORD ptr VerLanguageNameA_Original
VerLanguageNameA_EXPORT endp

VerLanguageNameW_EXPORT proc
  jmp QWORD ptr VerLanguageNameW_Original
VerLanguageNameW_EXPORT endp

VerQueryValueA_EXPORT proc
  jmp QWORD ptr VerQueryValueA_Original
VerQueryValueA_EXPORT endp

VerQueryValueW_EXPORT proc
  jmp QWORD ptr VerQueryValueW_Original
VerQueryValueW_EXPORT endp

ApplyCompatResolutionQuirking_EXPORT proc
  jmp QWORD ptr ApplyCompatResolutionQuirking_Original
ApplyCompatResolutionQuirking_EXPORT endp

CompatString_EXPORT proc
  jmp QWORD ptr CompatString_Original
CompatString_EXPORT endp

CompatValue_EXPORT proc
  jmp QWORD ptr CompatValue_Original
CompatValue_EXPORT endp

DXGIDumpJournal_EXPORT proc
  jmp QWORD ptr DXGIDumpJournal_Original
DXGIDumpJournal_EXPORT endp

PIXBeginCapture_EXPORT proc
  jmp QWORD ptr PIXBeginCapture_Original
PIXBeginCapture_EXPORT endp

PIXEndCapture_EXPORT proc
  jmp QWORD ptr PIXEndCapture_Original
PIXEndCapture_EXPORT endp

PIXGetCaptureState_EXPORT proc
  jmp QWORD ptr PIXGetCaptureState_Original
PIXGetCaptureState_EXPORT endp

SetAppCompatStringPointer_EXPORT proc
  jmp QWORD ptr SetAppCompatStringPointer_Original
SetAppCompatStringPointer_EXPORT endp

UpdateHMDEmulationStatus_EXPORT proc
  jmp QWORD ptr UpdateHMDEmulationStatus_Original
UpdateHMDEmulationStatus_EXPORT endp

CreateDXGIFactory_EXPORT proc
  jmp QWORD ptr CreateDXGIFactory_Original
CreateDXGIFactory_EXPORT endp

CreateDXGIFactory1_EXPORT proc
  jmp QWORD ptr CreateDXGIFactory1_Original
CreateDXGIFactory1_EXPORT endp

CreateDXGIFactory2_EXPORT proc
  jmp QWORD ptr CreateDXGIFactory2_Original
CreateDXGIFactory2_EXPORT endp

DXGID3D10CreateDevice_EXPORT proc
  jmp QWORD ptr DXGID3D10CreateDevice_Original
DXGID3D10CreateDevice_EXPORT endp

DXGID3D10CreateLayeredDevice_EXPORT proc
  jmp QWORD ptr DXGID3D10CreateLayeredDevice_Original
DXGID3D10CreateLayeredDevice_EXPORT endp

DXGID3D10GetLayeredDeviceSize_EXPORT proc
  jmp QWORD ptr DXGID3D10GetLayeredDeviceSize_Original
DXGID3D10GetLayeredDeviceSize_EXPORT endp

DXGID3D10RegisterLayers_EXPORT proc
  jmp QWORD ptr DXGID3D10RegisterLayers_Original
DXGID3D10RegisterLayers_EXPORT endp

DXGIDeclareAdapterRemovalSupport_EXPORT proc
  jmp QWORD ptr DXGIDeclareAdapterRemovalSupport_Original
DXGIDeclareAdapterRemovalSupport_EXPORT endp

DXGIGetDebugInterface1_EXPORT proc
  jmp QWORD ptr DXGIGetDebugInterface1_Original
DXGIGetDebugInterface1_EXPORT endp

DXGIReportAdapterConfiguration_EXPORT proc
  jmp QWORD ptr DXGIReportAdapterConfiguration_Original
DXGIReportAdapterConfiguration_EXPORT endp

end
