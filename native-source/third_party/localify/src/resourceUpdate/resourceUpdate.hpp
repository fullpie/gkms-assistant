#pragma once

#include <string>

namespace GkmsResourceUpdate {
	void saveProgramConfig();
	std::string GetCurrentResourceVersion(bool useCache);
	std::string GetCurrentTextureVersion(bool useCache);
	void CheckUpdateFromAPI(bool isManual);
	void CheckTextureUpdateFromAPI(bool isManual);
	void checkUpdateFromURL(const std::string& downloadUrl);
}
