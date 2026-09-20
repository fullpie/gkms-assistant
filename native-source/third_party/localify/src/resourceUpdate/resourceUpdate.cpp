#include "stdinclude.hpp"
#include "cpprest/http_client.h"
#include "cpprest/filestream.h"
#include "nlohmann/json.hpp"
#include "GakumasLocalify/Log.h"
#include "gkmsGUI/GUII18n.hpp"
#include <atomic>
#include <algorithm>
#include <cctype>
#include <format>
#include <sstream>
#include "unzip.hpp"

extern std::filesystem::path gakumasLocalPath;
extern std::filesystem::path ProgramConfigJson;
extern bool downloading;
extern float downloadProgress;
extern std::function<void()> g_reload_all_data;
std::string resourceVersionCache = "";
std::string textureVersionCache = "";

namespace GkmsResourceUpdate {
	std::atomic_bool updateJobRunning = false;

	class UpdateJobGuard {
	public:
		explicit UpdateJobGuard(const char* jobName) : active(false) {
			bool expected = false;
			if (!updateJobRunning.compare_exchange_strong(expected, true)) {
				GakumasLocal::Log::InfoFmt("Skip %s: another resource update job is running.", jobName);
				return;
			}
			active = true;
			downloading = true;
			downloadProgress = 0.0f;
		}

		~UpdateJobGuard() {
			if (!active) return;
			downloading = false;
			updateJobRunning = false;
		}

		explicit operator bool() const {
			return active;
		}

	private:
		bool active;
	};

	std::string trimString(std::string content) {
		auto is_not_space = [](unsigned char ch) {
			return !std::isspace(ch);
		};
		content.erase(content.begin(), std::find_if(content.begin(), content.end(), is_not_space));
		content.erase(std::find_if(content.rbegin(), content.rend(), is_not_space).base(), content.end());
		return content;
	}

	std::string readTrimmedFile(const std::filesystem::path& filePath) {
		std::ifstream file(filePath);
		if (!file) {
			return "Unknown";
		}

		std::stringstream buffer;
		buffer << file.rdbuf();
		return trimString(buffer.str());
	}

	std::filesystem::path getTextureResourceRoot() {
		return gakumasLocalPath / "texture2d";
	}

	std::filesystem::path findTextureZipSourceDir(const std::filesystem::path& extractDir) {
		std::error_code ec;
		std::filesystem::path fallback;
		for (const auto& entry : std::filesystem::recursive_directory_iterator(
			extractDir, std::filesystem::directory_options::skip_permission_denied, ec)) {
			if (ec) break;
			if (!entry.is_regular_file(ec)) continue;
			if (entry.path().filename() != "texture_version.txt") continue;

			const auto parent = entry.path().parent_path();
			if (parent.filename() == "texture2d") {
				return parent;
			}
			if (fallback.empty()) {
				fallback = parent;
			}
		}
		return fallback;
	}

	void saveProgramConfig() {
		nlohmann::json config;
		config["enableConsole"] = g_enable_console;
		config["useRemoteAssets"] = g_useRemoteAssets;
		config["transRemoteZipUrl"] = g_remoteResourceUrl;
		config["useAPIAssets"] = g_useAPIAssets;
		config["useAPIAssetsURL"] = g_useAPIAssetsURL;
		config["useAPITextureAssets"] = g_useAPITextureAssets;
		config["useAPITextureAssetsURL"] = g_useAPITextureAssetsURL;
		config["delTextureRemoteAfterUpdate"] = g_delTextureRemoteAfterUpdate;

		std::ofstream out(ProgramConfigJson);
		if (!out) {
			GakumasLocal::Log::ErrorFmt("SaveProgramConfig error: Cannot open file: %s", ProgramConfigJson.c_str());
			return;
		}
		out << config.dump(4);
		GakumasLocal::Log::Info("SaveProgramConfig success");
	}

	web::http::http_response send_get(std::string url, int timeout) {
		web::http::client::http_client_config cfg;
		cfg.set_timeout(utility::seconds(30));
		web::http::client::http_client client(utility::conversions::to_utf16string(url), cfg);
		return client.request(web::http::methods::GET).get();
	}

	bool DownloadFile(const std::string& url, const std::string& outputPath) {
		using namespace utility;
		using namespace web;
		using namespace web::http;
		using namespace web::http::client;
		using namespace concurrency::streams;

		try {
			// 打开输出文件流（同步方式）
			auto outTask = fstream::open_ostream(conversions::to_string_t(outputPath));
			outTask.wait();
			auto fileStream = outTask.get();

			// 创建 HTTP 客户端，注意：如果 url 包含完整路径，cpprestsdk 会自动解析
			http_client client(conversions::to_string_t(url));

			downloading = true;
			downloadProgress = 0.0f;

			// 发起 GET 请求
			auto responseTask = client.request(methods::GET);
			responseTask.wait();
			http_response response = responseTask.get();
			if (response.status_code() != status_codes::OK) {
				downloading = false;
				GakumasLocal::Log::ErrorFmt("DownloadFile error: %d", response.status_code());
				return false;
			}

			// 获取响应头中的文件大小（如果存在）
			uint64_t contentLength = 0;
			if (response.headers().has(L"Content-Length"))
				contentLength = std::stoull(conversions::to_utf8string(response.headers().find(L"Content-Length")->second));

			// 读取响应体，逐块写入文件，同时更新进度
			auto inStream = response.body();
			const size_t bufferSize = 8192;
			// std::vector<unsigned char> buffer(bufferSize);
			size_t totalDownloaded = 0;

			while (true) {
				auto readTask = inStream.read(fileStream.streambuf(), bufferSize);
				readTask.wait();
				size_t bytesRead = readTask.get();
				if (bytesRead == 0)
					break;
				totalDownloaded += bytesRead;
				if (contentLength > 0)
					downloadProgress = static_cast<float>(totalDownloaded) / static_cast<float>(contentLength);
			}
			fileStream.close().wait();
			downloading = false;
			return true;
		}
		catch (const std::exception& e) {
			downloading = false;
			GakumasLocal::Log::ErrorFmt("DownloadFile error: %s", e.what());
			return false;
		}
	}

	std::string GetCurrentResourceVersion(bool useCache) {
		if (useCache) {
			if (!resourceVersionCache.empty()) {
				return resourceVersionCache;
			}
		}

		auto resourceVersionFile = gakumasLocalPath / "version.txt";
		resourceVersionCache = readTrimmedFile(resourceVersionFile);
		return resourceVersionCache;
	}

	std::string GetCurrentTextureVersion(bool useCache) {
		if (useCache) {
			if (!textureVersionCache.empty()) {
				return textureVersionCache;
			}
		}

		auto textureVersionFile = getTextureResourceRoot() / "texture_version.txt";
		textureVersionCache = readTrimmedFile(textureVersionFile);
		return textureVersionCache;
	}

	bool unzipFileFromURL(std::string downloadUrl, const std::string& unzipPath, const std::string& targetDir = "") {
		std::string tempZipFile = (gakumasLocalPath / "temp_download.zip").string();
		if (std::filesystem::exists(tempZipFile)) {
			std::filesystem::remove(tempZipFile);
		}
		if (!DownloadFile(downloadUrl, tempZipFile)) {
			GakumasLocal::Log::Error("Download zip file failed.");
			return false;
		}
		if (!UnzipFile(tempZipFile, unzipPath, targetDir)) {
			GakumasLocal::Log::Error("Unzip file failed.");
			return false;
		}
		return true;
	}

	bool installTextureZipFromURL(const std::string& downloadUrl, const std::string& expectedVersion) {
		auto textureRoot = getTextureResourceRoot();
		auto tempZipFile = gakumasLocalPath / "temp_texture_download.zip";
		auto extractDir = gakumasLocalPath / "texture2d.extract";
		auto installDir = gakumasLocalPath / "texture2d.tmp";

		std::error_code ec;
		std::filesystem::remove(tempZipFile, ec);
		std::filesystem::remove_all(extractDir, ec);
		std::filesystem::remove_all(installDir, ec);
		std::filesystem::create_directories(gakumasLocalPath, ec);

		if (!DownloadFile(downloadUrl, tempZipFile.string())) {
			GakumasLocal::Log::Error("Download texture zip file failed.");
			return false;
		}

		if (!UnzipFile(tempZipFile.string(), extractDir.string())) {
			GakumasLocal::Log::Error("Unzip texture file failed.");
			if (g_delTextureRemoteAfterUpdate) {
				std::filesystem::remove(tempZipFile, ec);
			}
			return false;
		}

		auto sourceDir = findTextureZipSourceDir(extractDir);
		if (sourceDir.empty()) {
			GakumasLocal::Log::Error("Texture zip validation failed: texture_version.txt not found.");
			std::filesystem::remove_all(extractDir, ec);
			if (g_delTextureRemoteAfterUpdate) {
				std::filesystem::remove(tempZipFile, ec);
			}
			return false;
		}

		auto versionFile = sourceDir / "texture_version.txt";
		const auto packageVersion = readTrimmedFile(versionFile);
		if (packageVersion.empty() || packageVersion == "Unknown") {
			GakumasLocal::Log::Error("Texture zip validation failed: texture_version.txt is empty or not found.");
			std::filesystem::remove_all(extractDir, ec);
			if (g_delTextureRemoteAfterUpdate) {
				std::filesystem::remove(tempZipFile, ec);
			}
			return false;
		}
		if (!expectedVersion.empty() && packageVersion != expectedVersion) {
			GakumasLocal::Log::ErrorFmt(
				"Texture zip validation failed: texture_version.txt (%s) differs from release tag (%s).",
				packageVersion.c_str(), expectedVersion.c_str());
			std::filesystem::remove_all(extractDir, ec);
			if (g_delTextureRemoteAfterUpdate) {
				std::filesystem::remove(tempZipFile, ec);
			}
			return false;
		}

		std::filesystem::copy(sourceDir, installDir,
			std::filesystem::copy_options::recursive | std::filesystem::copy_options::overwrite_existing, ec);
		if (ec) {
			GakumasLocal::Log::ErrorFmt("Copy texture resource failed: %s", ec.message().c_str());
			std::filesystem::remove_all(extractDir, ec);
			if (g_delTextureRemoteAfterUpdate) {
				std::filesystem::remove(tempZipFile, ec);
			}
			return false;
		}

		std::filesystem::remove_all(textureRoot, ec);
		ec.clear();
		std::filesystem::rename(installDir, textureRoot, ec);
		if (ec) {
			GakumasLocal::Log::ErrorFmt("Install texture resource failed: %s", ec.message().c_str());
			std::filesystem::remove_all(installDir, ec);
			std::filesystem::remove_all(extractDir, ec);
			if (g_delTextureRemoteAfterUpdate) {
				std::filesystem::remove(tempZipFile, ec);
			}
			return false;
		}

		std::filesystem::remove_all(extractDir, ec);
		if (g_delTextureRemoteAfterUpdate) {
			std::filesystem::remove(tempZipFile, ec);
		}

		textureVersionCache.clear();
		const auto installedVersion = GetCurrentTextureVersion(false);
		GakumasLocal::Log::InfoFmt("Texture zip installed into %s, version=%s",
			textureRoot.string().c_str(), installedVersion.c_str());
		return true;
	}

	void CheckUpdateFromAPI(bool isManual) {
		std::thread([isManual]() {
			try {
				if (!g_useAPIAssets) {
					return;
				}
				UpdateJobGuard job("resource API update");
				if (!job) return;

				GakumasLocal::Log::Info("Checking update from API...");

				auto response = send_get(g_useAPIAssetsURL, 30);
				if (response.status_code() != 200) {
					GakumasLocal::Log::ErrorFmt("Failed to check update from API: %d\n", response.status_code());
					return;
				}

				auto data = nlohmann::json::parse(response.extract_utf8string().get());

				std::string remoteVersion = data["tag_name"];
				const auto localVersion = GetCurrentResourceVersion(false);

				if (localVersion == remoteVersion) {
					if (isManual) {
						auto check = MessageBoxW(NULL, utility::conversions::to_string_t(GkmsGUII18n::ts("local_file_already_latest")).c_str(),
							L"Check Update", MB_OKCANCEL);
						if (check != IDOK) {
							return;
						}
					}
					else {
						return;
					}
				}

				std::string description = data["body"];

				auto check = MessageBoxW(NULL, std::format(L"{} -> {}\n\n{}", utility::conversions::to_string_t(localVersion), 
					utility::conversions::to_string_t(remoteVersion), utility::conversions::to_string_t(description)).c_str(),
					L"Resource Update", MB_OKCANCEL);
				if (check != IDOK) {
					return;
				}


				if (!data.contains("assets") || !data["assets"].is_array()) {
					GakumasLocal::Log::Error("API response doesn't contain assets array.");
					return;
				}
				for (const auto& asset : data["assets"]) {
					if (!asset.contains("name") || !asset.contains("browser_download_url"))
						continue;
					std::string name = asset["name"];
					if (name.ends_with(".zip")) {
						std::string downloadUrl = asset["browser_download_url"];

						if (unzipFileFromURL(downloadUrl, gakumasLocalPath.string())) {
							g_reload_all_data();
							GakumasLocal::Log::Info("Update completed.");
						}
						// 仅解压一个文件
						return;
					}
				}
				GakumasLocal::Log::Error("No .zip file found.");

			}
			catch (std::exception& e) {
				GakumasLocal::Log::ErrorFmt("Exception occurred in CheckUpdateFromAPI: %s\n", e.what());
			}
			}).detach();
	}

	void CheckTextureUpdateFromAPI(bool isManual) {
		std::thread([isManual]() {
			try {
				if (!g_useAPITextureAssets) {
					return;
				}
				UpdateJobGuard job("texture API update");
				if (!job) return;

				GakumasLocal::Log::Info("Checking texture update from API...");

				auto response = send_get(g_useAPITextureAssetsURL, 30);
				if (response.status_code() != 200) {
					GakumasLocal::Log::ErrorFmt("Failed to check texture update from API: %d\n", response.status_code());
					return;
				}

				auto data = nlohmann::json::parse(response.extract_utf8string().get());
				if (!data.contains("tag_name") || !data["tag_name"].is_string()) {
					GakumasLocal::Log::Error("Texture API response doesn't contain tag_name.");
					return;
				}

				std::string remoteVersion = data["tag_name"];
				const auto localVersion = GetCurrentTextureVersion(false);

				if (localVersion == remoteVersion) {
					if (isManual) {
						auto check = MessageBoxW(NULL, utility::conversions::to_string_t(GkmsGUII18n::ts("local_file_already_latest")).c_str(),
							L"Texture Resource Update", MB_OKCANCEL);
						if (check != IDOK) {
							return;
						}
					}
					else {
						return;
					}
				}

				std::string description = "";
				if (data.contains("body") && data["body"].is_string()) {
					description = data["body"];
				}

				auto check = MessageBoxW(NULL, std::format(L"{} -> {}\n\n{}", utility::conversions::to_string_t(localVersion),
					utility::conversions::to_string_t(remoteVersion), utility::conversions::to_string_t(description)).c_str(),
					L"Texture Resource Update", MB_OKCANCEL);
				if (check != IDOK) {
					return;
				}

				if (!data.contains("assets") || !data["assets"].is_array()) {
					GakumasLocal::Log::Error("Texture API response doesn't contain assets array.");
					return;
				}

				std::string downloadUrl = "";
				for (const auto& asset : data["assets"]) {
					if (!asset.contains("name") || !asset.contains("browser_download_url") || !asset["name"].is_string() || !asset["browser_download_url"].is_string()) {
						continue;
					}
					std::string name = asset["name"];
					std::string lowerName = name;
					std::transform(lowerName.begin(), lowerName.end(), lowerName.begin(), [](unsigned char ch) {
						return static_cast<char>(std::tolower(ch));
					});
					if (lowerName.ends_with(".zip")) {
						downloadUrl = asset["browser_download_url"];
						if (lowerName == "texture2d.zip") {
							break;
						}
					}
				}

				if (downloadUrl.empty()) {
					GakumasLocal::Log::Error("No texture .zip file found.");
					return;
				}

				if (installTextureZipFromURL(downloadUrl, remoteVersion)) {
					g_reload_all_data();
					GakumasLocal::Log::Info("Texture update completed.");
				}
			}
			catch (std::exception& e) {
				GakumasLocal::Log::ErrorFmt("Exception occurred in CheckTextureUpdateFromAPI: %s\n", e.what());
			}
			}).detach();
	}

	void checkUpdateFromURL(const std::string& downloadUrl) {
		std::thread([downloadUrl]() {
			UpdateJobGuard job("remote zip update");
			if (!job) return;
			if (unzipFileFromURL(downloadUrl, gakumasLocalPath.string(), "local-files")) {
				g_reload_all_data();
				GakumasLocal::Log::Info("Update completed.");
			}
			}).detach();
	}
}
