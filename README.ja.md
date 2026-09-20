# GKMS Assistant

[繁體中文](README.md) · [English](README.en.md) · **日本語**

『学園アイドルマスター』の **Windows 向け自動育成アシスタント**です。シンプルな GUI でアイドル・モード・連続育成回数を選び、モデルによる自動育成と進行状況の確認ができます。

**現在のバージョン：0.1** · **[最新版をダウンロード](https://github.com/fullpie/gkms-assistant/releases/latest)**

<!-- GUI screenshot pending: add a verified application capture here; do not use a mockup or an image containing account data. -->

## 主な機能

- **自動育成**：N.I.A. Pro／Master、アイドル選択、連続育成、進行状況の表示。
- **2 種類の方策**：メイン BC（行動模倣）モデル＋補助戦略と、統合型 BC モデルを切り替え可能。
- **3 言語の UI**：繁体字中国語・英語・日本語に対応。必須モジュールの導入と、任意のゲーム翻訳機能を搭載。

> 現在、モデルの学習と主なオフライン検証は「全力」の N.I.A. Pro／Master が中心です。他のタイプも選択できますが、同等の学習・検証は完了していません。

## 使い方

**動作要件：** Windows x64、Microsoft Edge WebView2 Runtime、.NET Framework 4.7.2 以降。

1. [Releases](https://github.com/fullpie/gkms-assistant/releases/latest) から通常利用向けの `gkms-assistant-0.1.0-windows-x64.zip` をダウンロードします。GUI 更新用・ソースコード用のアーカイブとは異なります。
2. すべて展開してから `GKMS-Assistant.exe` を起動します。
3. 設定でゲームフォルダーを指定し、必須の操作モジュールをインストールしてから育成を開始します。ゲーム翻訳の導入は任意です。

## 今後の目標

- **強化学習（RL）**：RL による学習を導入し、育成中の判断とスコアの改善を目指します。
- **全モード対応**：すべての育成モード・タイプへ段階的に対応を広げ、各モードの方策と検証を充実させます。

上記は開発目標であり、0.1 で実装済みの機能ではありません。

## 開発・ライセンス

[開発・パッケージングの説明（英語）](docs/development.md) · [ライセンスに関する注意事項（英語）](NOTICE.txt)

独自部分の権利はすべて留保されており、追加のオープンソースライセンスは付与していません。第三者コンポーネントには、それぞれのライセンスが引き続き適用されます。
