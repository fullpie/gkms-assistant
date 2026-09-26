# GKMS Assistant 0.2.0

**繁體中文** · [English](README.en.md) · [日本語](README.ja.md)

學園偶像大師 PC 版的自動培育助手。以深度學習模型決定演出出牌、飲料與附屬選擇，透過 DLL 讀取遊戲狀態並執行操作。

<!-- BEGIN GUI SCREENSHOTS -->
<details>
<summary>介面示意</summary>

![自動培育介面](docs/images/gui-cultivation-zh-Hant.png)
![設定與安裝畫面](docs/images/gui-setup-zh-Hant.png)

保留的舊版離線示範截圖，數值不是實測結果；0.2 的演出模型已改為共用 RL。
</details>
<!-- END GUI SCREENSHOTS -->

## 功能

- 自動完成選角、培育、演出、結算與回首頁。
- 一個共用 RL 模型涵蓋六種流派，以流派、模式及演出階段作為輸入。
- 支援卡與回憶卡使用現有遊戲推薦／規則編成，可指定鎖定卡片。
- 顯示目前模型、執行進度與停止原因；整合遊戲啟動、翻譯及操作元件管理。
- 顯示演出預估分數；目前尚未校準，只作參考。
- 開啟時檢查助手更新，由使用者決定是否安裝。

本版模型範圍為 N.I.A. Pro／Master；各流派與模式的實機驗證仍有差異，部分附屬決策的訓練資料不足。目前尚未證明穩定優於舊 BC，不保證通關或高分。培育週行動沿用既有策略，演出使用固定 RL 權重，不會在遊玩中自行訓練或換版。舊 BC 已停用選取。

## 使用

1. 從 [Releases](https://github.com/fullpie/gkms-assistant/releases) 下載 Windows 版，完整解壓縮。
2. 執行 `GKMS-Assistant.exe`，依介面設定遊戲位置並安裝所需元件。
3. 選擇角色、模式與場數，確認編成後開始。

介面使用獨立 Windows 視窗，需要 Microsoft Edge WebView2 Runtime。首次啟動或維護遊戲可能要求系統管理員權限；助手平常以一般權限執行。更新後若無法開啟，可使用包內的「恢復 GUI.cmd」。請保留整個資料夾，勿單獨搬移 EXE。

## 範圍與資料

公開版不包含私人研究介面、訓練回放、帳號資料或測試 DLL。私人版的原生模擬編成搜尋不包含在本版。模型與推論依賴隨發布包提供，使用者不必安裝 Python 或具備 NVIDIA 顯示卡。

原創部分保留所有權利；第三方元件依各自授權提供。開發者建置資訊見 [公開版打包說明](docs/public-gui-packaging.md)。
