#include "stdinclude.hpp"
#include "i18nData/strings_en.hpp"
#include "i18nData/strings_ja.hpp"
#include "i18nData/strings_zh-rCN.hpp"
#include "i18nData/strings_zh-rTW.hpp"


namespace GkmsGUII18n {
	const LANGID localLanguage = GetUserDefaultUILanguage();

	std::unordered_set<LANGID> sChineseLangIds{ { 0x0004, 0x0804, 0x1004 } };  // zh-Hans, zh-CN, zh-SG
	std::unordered_set<LANGID> tChineseLangIds{ { 0x0404, 0x0c04, 0x1404, 0x048E } };  // zh-TW, zh-HK, zh-MO, zh-yue-HK
	std::unordered_set<LANGID> jpnLangIds{ { 0x0011, 0x0411 } };  // ja, ja-JP

	std::unordered_map<std::string, std::string> GetI18nData() {
		if (sChineseLangIds.contains(localLanguage)) {
			return I18nData::i18nData_zh_rCN;
		}
		else if (tChineseLangIds.contains(localLanguage)) {
			return I18nData::i18nData_zh_rTW;
		}
		else if (jpnLangIds.contains(localLanguage)) {
			return I18nData::i18nData_ja;
		}
		else {
			return I18nData::i18nData_default;
		}
	}

    const char* ts(const std::string& key) {
        static std::unordered_map<std::string, std::string> i18nData = GetI18nData();

        if (auto it = i18nData.find(key); it != i18nData.end()) {
            return it->second.c_str();
        }
        if (auto it = I18nData::i18nData_default.find(key); it != I18nData::i18nData_default.end()) {
            return it->second.c_str();
        }

        static std::unordered_map<std::string, std::string> fallbackMap;
        auto [iter, inserted] = fallbackMap.emplace(key, key);
        return iter->second.c_str();
    }

}
