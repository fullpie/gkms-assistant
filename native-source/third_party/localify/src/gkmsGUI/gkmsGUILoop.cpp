#include "imgui/imgui.h"
#include "stdinclude.hpp"
#include "GUII18n.hpp"
#include "GakumasLocalify/config/Config.hpp"
#include "resourceUpdate/resourceUpdate.hpp"
#include "platformDefine.hpp"

extern void* SetResolution_orig;
extern std::function<void()> g_reload_all_data;
extern std::filesystem::path ConfigJson;

bool downloading = false;
float downloadProgress = 0.0f;

#define INPUT_AND_SLIDER_FLOAT(label, data, min, max) \
    ImGui::SetNextItemWidth(inputFloatWidth);\
    ImGui::InputFloat("##"##label, data);\
    ImGui::SameLine();\
    ImGui::SetNextItemWidth(ImGui::CalcItemWidth() - inputFloatWidth - 1.0f);\
    ImGui::SliderFloat(label, data, min, max)

#define FOR_INPUT_AND_SLIDER_FLOAT(label, data, min, max, hideIdName) \
    ImGui::SetNextItemWidth(inputFloatWidth);\
    ImGui::InputFloat(("##"##label + hideIdName).c_str(), data);\
    ImGui::SameLine();\
    ImGui::SetNextItemWidth(ImGui::CalcItemWidth() - inputFloatWidth - 1.0f);\
    ImGui::SliderFloat((label##"##" + hideIdName).c_str(), data, min, max)

#define INPUT_AND_SLIDER_INT(label, data, min, max) \
    ImGui::SetNextItemWidth(inputFloatWidth);\
    ImGui::InputInt((std::string("##") + label).c_str(), data);\
    ImGui::SameLine();\
    ImGui::SetNextItemWidth(ImGui::CalcItemWidth() - inputFloatWidth - 1.0f);\
    ImGui::SliderInt(label, data, min, max)

#define InputFloatWithItemWidth(label, flt) \
    ImGui::PushItemWidth(itemWidth); \
    ImGui::InputFloat(label, flt); \
    ImGui::PopItemWidth()
    

#define HELP_TOOLTIP(label, text) \
    ImGui::TextDisabled(label); \
    if (ImGui::IsItemHovered()) { \
        ImGui::BeginTooltip(); \
        ImGui::Text(text); \
        ImGui::EndTooltip(); \
    }


// 回调函数，用于处理字符串大小变化
static int InputTextCallback(ImGuiInputTextCallbackData * data)
{
    if (data->EventFlag == ImGuiInputTextFlags_CallbackResize)
    {
        // 当用户输入导致需要扩容时，会调用该回调
        std::string* str = static_cast<std::string*>(data->UserData);
        IM_ASSERT(data->Buf == str->c_str());
        // 根据新的长度调整 std::string 大小
        str->resize(data->BufTextLen);
        // 更新回调数据中的缓冲区指针
        data->Buf = const_cast<char*>(str->c_str());
    }
    return 0;
}

// 封装的 InputText 函数，可以直接编辑 std::string
bool InputTextString(const char* label, std::string* str, ImGuiInputTextFlags flags = 0)
{
    // 如果字符串为空，则确保有至少一个字符空间（空字符）
    if (str->empty())
        *str = "";
    // 调用 ImGui::InputText，并通过回调处理动态扩容
    return ImGui::InputText(label,
        // 注意：需要获得非 const 的指针，c_str() 返回的是 const char*
        const_cast<char*>(str->c_str()),
        // 使用当前字符串容量加 1 的空间（注意：初始容量可能较小）
        str->capacity() + 1,
        flags | ImGuiInputTextFlags_CallbackResize,
        InputTextCallback,
        static_cast<void*>(str));
}

void onBClickPresetChanged(int index) {
    using namespace GakumasLocal;

    std::vector<float> setData;
    switch (index) {
    case 0:
        setData = { 0.33f, 0.07f, 0.7f, 0.06f, 0.25f, 0.2f, 0.5f,
                   1.0f, 1.0f, 1.0f, 1.0f, 1.0f, 1.0f, 1.0f };
        break;
    case 1:
        setData = { 0.365f, 0.06f, 0.62f, 0.07f, 0.25f, 0.2f, 0.5f,
                   1.0f, 1.5f, 1.5f, 1.5f, 1.5f, 1.5f, 1.5f };
        break;
    case 2:
        setData = { 0.4f, 0.065f, 0.55f, 0.075f, 0.25f, 0.2f, 0.5f,
                   1.0f, 2.0f, 2.0f, 2.0f, 2.0f, 2.0f, 2.0f };
        break;
    case 3:
        setData = { 0.4f, 0.065f, 0.55f, 0.075f, 0.25f, 0.2f, 0.5f,
                   1.0f, 4.0f, 4.0f, 4.0f, 4.0f, 4.0f, 3.0f };
        break;
    case 4:
        setData = { 0.4f, 0.06f, 0.4f, 0.075f, 0.55f, 0.2f, 0.8f,
                   1.0f, 6.0f, 6.0f, 6.0f, 6.0f, 6.0f, 3.5f };
        break;
    case 5:
        setData = { 0.33f, 0.08f, 0.8f, 0.12f, 0.55f, 0.2f, 1.0f,
                   0.0f };
        break;
    default:
        setData = { 0.33f, 0.08f, 1.0f, 0.055f, 0.15f, 0.2f, 0.5f,
                   1.0f, 1.0f, 1.0f, 1.0f, 1.0f, 1.0f, 1.0f };
        break;
    }

    Config::bDamping = setData[0];
    Config::bStiffness = setData[1];
    Config::bSpring = setData[2];
    Config::bPendulum = setData[3];
    Config::bPendulumRange = setData[4];
    Config::bAverage = setData[5];
    Config::bRootWeight = setData[6];

    if (setData.size() > 7 && setData[7] != 0.0f && setData.size() >= 14) {
        Config::bLimitXx = setData[8];
        Config::bLimitXy = setData[9];
        Config::bLimitYx = setData[10];
        Config::bLimitYy = setData[11];
        Config::bLimitZx = setData[12];
        Config::bLimitZy = setData[13];
        Config::bUseLimit = true;
    }
    else {
        Config::bUseLimit = false;
    }

    Config::bUseArmCorrection = true;
}


namespace GkmsGUILoop {
    static float inputFloatWidth = 50.0f;
    static float indentWidth = 10.0f;


	void mainLoop() {
        using namespace GkmsGUII18n;
		using namespace GakumasLocal;

        if (ImGui::Begin("Gakumas Plugin Config")) {
			ImGui::Text("Plugin Version: %s", PLUGIN_VERSION);
			ImGui::Text("Resource Version: %s", GkmsResourceUpdate::GetCurrentResourceVersion(true).c_str());
			ImGui::Text("%s: %s", ts("texture_version"), GkmsResourceUpdate::GetCurrentTextureVersion(true).c_str());

            if (ImGui::Button("Reload Config And Translation Data")) {
                g_reload_all_data();
            }

            if (ImGui::Button(ts("Save Config"))) {
				Config::SaveConfig(ConfigJson.string());
				GkmsResourceUpdate::saveProgramConfig();
            }

			// 基础设置
            if (ImGui::CollapsingHeader(ts("basic_settings"), ImGuiTreeNodeFlags_DefaultOpen)) {
                ImGui::Indent(indentWidth);

                ImGui::Checkbox(ts("lazy_init"), &Config::lazyInit);
                ImGui::Checkbox(ts("replace_font"), &Config::replaceFont);
                ImGui::Checkbox(ts("replace_texture"), &Config::replaceTexture);

                ImGui::Unindent(indentWidth);
            }

            // 资源设置
            if (ImGui::CollapsingHeader((std::string(ts("resource_settings")) + "##Header").c_str(), ImGuiTreeNodeFlags_None)) {
                ImGui::Indent(indentWidth);

                if (ImGui::Checkbox(ts("check_resource_from_api"), &g_useAPIAssets)) {
                    if (g_useAPIAssets) g_useRemoteAssets = false;
                }
                if (g_useAPIAssets) {
                    InputTextString(ts("api_addr"), &g_useAPIAssetsURL);
                    if (!downloading && ImGui::Button((std::string(ts("check_update")) + "##APIResource").c_str())) {
						GkmsResourceUpdate::CheckUpdateFromAPI(true);
                    }
                    if (downloading) {
                        ImGui::ProgressBar(downloadProgress);
                        ImGui::SameLine();
                        ImGui::Text("Downloading");
                    }
                }

                if (ImGui::Checkbox(ts("use_remote_zip_resource"), &g_useRemoteAssets)) {
					if (g_useRemoteAssets) g_useAPIAssets = false;
                }
                if (g_useRemoteAssets) {
                    InputTextString(ts("resource_url"), &g_remoteResourceUrl);
                    if (!downloading && ImGui::Button((std::string(ts("download")) + "##RemoteResource").c_str())) {
						GkmsResourceUpdate::checkUpdateFromURL(g_remoteResourceUrl);
                    }
                    if (downloading) {
                        ImGui::ProgressBar(downloadProgress);
                        ImGui::SameLine();
                        ImGui::Text("Downloading");
                    }
                }

                if (Config::replaceTexture) {
                    ImGui::Separator();
                    ImGui::Text("%s: %s", ts("downloaded_texture_version"), GkmsResourceUpdate::GetCurrentTextureVersion(true).c_str());
                    ImGui::Checkbox(ts("check_texture_from_api"), &g_useAPITextureAssets);
                    if (g_useAPITextureAssets) {
                        ImGui::Checkbox(ts("del_texture_remote_after_update"), &g_delTextureRemoteAfterUpdate);
                        InputTextString(ts("texture_api_addr"), &g_useAPITextureAssetsURL);
                        if (!downloading && ImGui::Button((std::string(ts("check_update")) + "##APITexture").c_str())) {
                            GkmsResourceUpdate::CheckTextureUpdateFromAPI(true);
                        }
                        if (downloading) {
                            ImGui::ProgressBar(downloadProgress);
                            ImGui::SameLine();
                            ImGui::Text("Downloading");
                        }
                    }
                }

                ImGui::Unindent(indentWidth);
            }

            // 画面设置
            if (ImGui::CollapsingHeader(ts("graphic_settings"), ImGuiTreeNodeFlags_DefaultOpen)) {
                ImGui::Indent(indentWidth);

				INPUT_AND_SLIDER_INT(ts("setFpsTitle"), &Config::targetFrameRate, 0, 540);

                if (ImGui::CollapsingHeader(ts("orientation_lock"), ImGuiTreeNodeFlags_DefaultOpen)) {
                    ImGui::RadioButton(ts("orientation_orig"), &Config::gameOrientation, 0);
                    ImGui::SameLine();
                    ImGui::RadioButton(ts("orientation_portrait"), &Config::gameOrientation, 1);
                    ImGui::SameLine();
                    ImGui::RadioButton(ts("orientation_landscape"), &Config::gameOrientation, 2);
                    ImGui::SameLine();
					ImGui::Checkbox(ts("dmmUnlockSize"), &Config::dmmUnlockSize);
                    ImGui::SameLine();
					HELP_TOOLTIP("(?)", ts("dmmUnlockSizeHelp"));
                }
                
                if (ImGui::CollapsingHeader((std::string(ts("useCustomeGraphicSettings")) + "##customeGraphicSettings1").c_str(), ImGuiTreeNodeFlags_DefaultOpen)) {
                    ImGui::Indent(indentWidth);

                    ImGui::Checkbox(ts("useCustomeGraphicSettings"), &Config::useCustomeGraphicSettings);

                    if (Config::useCustomeGraphicSettings) {
                        if (ImGui::Button(ts("max_high"))) {
                            Config::renderScale = 1.0f;
                            Config::qualitySettingsLevel = 5;
                            Config::volumeIndex = 4;
                            Config::maxBufferPixel = 8190;
                            Config::lodQualityLevel = 5;
                            Config::reflectionQualityLevel = 5;
                        }
                        ImGui::SameLine();
                        if (ImGui::Button(ts("very_high"))) {
                            Config::renderScale = 0.77f;
                            Config::qualitySettingsLevel = 3;
                            Config::volumeIndex = 3;
                            Config::maxBufferPixel = 3384;
                            Config::lodQualityLevel = 4;
                            Config::reflectionQualityLevel = 4;
                        }
                        ImGui::SameLine();
                        if (ImGui::Button(ts("hign"))) {
                            Config::renderScale = 0.67f;
                            Config::qualitySettingsLevel = 2;
                            Config::volumeIndex = 2;
                            Config::maxBufferPixel = 2538;
                            Config::lodQualityLevel = 3;
                            Config::reflectionQualityLevel = 3;
                        }
                        ImGui::SameLine();
                        if (ImGui::Button(ts("middle"))) {
                            Config::renderScale = 0.59f;
                            Config::qualitySettingsLevel = 1;
                            Config::volumeIndex = 1;
                            Config::maxBufferPixel = 1440;
                            Config::lodQualityLevel = 2;
                            Config::reflectionQualityLevel = 2;
                        }
                        ImGui::SameLine();
                        if (ImGui::Button(ts("low"))) {
                            Config::renderScale = 0.5f;
                            Config::qualitySettingsLevel = 1;
                            Config::volumeIndex = 0;
                            Config::maxBufferPixel = 1024;
                            Config::lodQualityLevel = 1;
                            Config::reflectionQualityLevel = 1;
                        }
                        ImGui::Separator();

                        ImGui::SliderFloat(ts("renderscale"), &Config::renderScale, 0, 5);
                        ImGui::SliderInt("QualityLevel (1/1/2/3/5)", &Config::qualitySettingsLevel, 1, 5);
                        ImGui::SliderInt("VolumeIndex (0/1/2/3/4)", &Config::volumeIndex, 0, 4);
                        ImGui::SliderInt("MaxBufferPixel (1024/1440/2538/3384/8190)", &Config::maxBufferPixel, 0, 16380);
                        ImGui::SliderInt("ReflectionLevel (0~5)", &Config::reflectionQualityLevel, 0, 5);
                        ImGui::SliderInt("LOD Level (0~5)", &Config::lodQualityLevel, 0, 5);
                    }

                    ImGui::Unindent(indentWidth);
                }

                ImGui::Unindent(indentWidth);
            }

			// 摄像机设置
            if (ImGui::CollapsingHeader(ts("camera_settings"), ImGuiTreeNodeFlags_DefaultOpen)) {
                ImGui::Indent(indentWidth);
                ImGui::Checkbox(ts("enable_free_camera"), &Config::enableFreeCamera);
                ImGui::Unindent(indentWidth);
            }

            // 调试设置
			if (ImGui::CollapsingHeader(ts("debug_settings"), ImGuiTreeNodeFlags_DefaultOpen)) {
				ImGui::Indent(indentWidth);

				ImGui::Checkbox(ts("useMasterDBTrans"), &Config::useMasterTrans);
				ImGui::Checkbox(ts("text_hook_test_mode"), &Config::textTest);
				ImGui::Checkbox(ts("export_text"), &Config::dumpText);
				ImGui::Checkbox(ts("dump_runtime_texture"), &Config::dumpRuntimeTexture);
				ImGui::Checkbox(ts("dump_leaderboard_replay"), &Config::dumpLeaderboardReplay);
				ImGui::Checkbox(ts("login_as_ios"), &Config::loginAsIOS);

				ImGui::Unindent(indentWidth);
			}

			// 胸部参数，失效
            if (Config::enableBreastParam && ImGui::CollapsingHeader((std::string(ts("enable_breast_param")) + "##Header").c_str(), ImGuiTreeNodeFlags_None)) {
                ImGui::Indent(indentWidth);

                ImGui::Checkbox(ts("enable_breast_param"), &Config::enableBreastParam);

                if (ImGui::Button("??##Breast")) {
                    onBClickPresetChanged(5);
                }
				ImGui::SameLine();
                if (ImGui::Button("+5##Breast")) {
                    onBClickPresetChanged(4);
                }
                ImGui::SameLine();
				if (ImGui::Button("+4##Breast")) {
					onBClickPresetChanged(3);
				}   
				ImGui::SameLine();
				if (ImGui::Button("+3##Breast")) {
					onBClickPresetChanged(2);
				}
				ImGui::SameLine();
				if (ImGui::Button("+2##Breast")) {
					onBClickPresetChanged(1);
				}
				ImGui::SameLine();
				if (ImGui::Button("+1##Breast")) {
					onBClickPresetChanged(0);
				}

                float availWidth = ImGui::GetContentRegionAvail().x;
                float spacing = ImGui::GetStyle().ItemSpacing.x;
                float itemWidth = (availWidth - spacing) / 4.0f;

                InputFloatWithItemWidth(ts("damping"), &Config::bDamping);
                ImGui::SameLine();
                InputFloatWithItemWidth(ts("stiffness"), &Config::bStiffness);

                InputFloatWithItemWidth(ts("spring"), &Config::bSpring);
                ImGui::SameLine();
                InputFloatWithItemWidth(ts("pendulum"), &Config::bPendulum);

                InputFloatWithItemWidth(ts("pendulumrange"), &Config::bPendulumRange);
                ImGui::SameLine();
                InputFloatWithItemWidth(ts("average"), &Config::bAverage);

                InputFloatWithItemWidth(ts("rootweight"), &Config::bRootWeight);

				ImGui::Checkbox(ts("##bUseScale"), &Config::bUseScale);
				ImGui::SameLine();
				ImGui::InputFloat(ts("breast_scale"), &Config::bScale);

				ImGui::Checkbox(ts("usearmcorrection"), &Config::bUseArmCorrection);

				ImGui::Checkbox(ts("uselimit_0_1"), &Config::bUseLimit);
                if (Config::bUseLimit) {
                    float availWidth = ImGui::GetContentRegionAvail().x;
                    float spacing = ImGui::GetStyle().ItemSpacing.x;
                    float itemWidth = (availWidth - spacing) / 6.0f;

                    InputFloatWithItemWidth(ts("axisx_x"), &Config::bLimitXx);
                    ImGui::SameLine();
                    InputFloatWithItemWidth(ts("axisy_x"), &Config::bLimitYx);
                    ImGui::SameLine();
                    InputFloatWithItemWidth(ts("axisz_x"), &Config::bLimitZx);

                    InputFloatWithItemWidth(ts("axisx_y"), &Config::bLimitXy);
                    ImGui::SameLine();
                    InputFloatWithItemWidth(ts("axisy_y"), &Config::bLimitYy);
                    ImGui::SameLine();
                    InputFloatWithItemWidth(ts("axisz_y"), &Config::bLimitZy);
                }

                ImGui::Unindent(indentWidth);
            }

			// 测试模式
            if (Config::dbgMode) {
                if (ImGui::CollapsingHeader(ts("test_mode_live"), ImGuiTreeNodeFlags_None)) {
                    ImGui::Indent(indentWidth);

                    ImGui::Checkbox(ts("unlockAllLive"), &Config::unlockAllLive);
                    ImGui::Checkbox(ts("unlockAllLiveCostume"), &Config::unlockAllLiveCostume);
                    /*ImGui::Checkbox(ts("liveUseCustomeDress"), &Config::enableLiveCustomeDress);
                    if (Config::enableLiveCustomeDress) {
                        InputTextString(ts("live_costume_head_id"), &Config::liveCustomeHeadId);
                        InputTextString(ts("live_custome_dress_id"), &Config::liveCustomeCostumeId);
                    }*/

                    ImGui::Unindent(indentWidth);
                }
            }

        }

        ImGui::End();
	}
}
