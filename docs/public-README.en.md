# GKMS Assistant {{VERSION}}

[繁體中文](README.md) · **English** · [日本語](README.ja.md)

An automated cultivation assistant for the PC version of Gakuen Idolmaster. The shared **RL_v1** model recommends support and memory setups, ranks supported cultivation choices, and handles exam cards, drinks and secondary choices. The existing DLL reads game state and executes each action.

<!-- BEGIN GUI SCREENSHOTS -->
<details>
<summary>Interface preview</summary>

![Automated cultivation](docs/images/gui-cultivation-en.png)
![Setup and installation](docs/images/gui-setup-en.png)

Retained screenshots from the earlier offline demo; displayed values are not measured results. Version 0.2 uses the shared RL exam model.
</details>
<!-- END GUI SCREENSHOTS -->

## Features

- Idol selection, cultivation, exams, rewards and return to Home.
- One shared exam model for six play styles, conditioned on mode and exam stage.
- Model recommendations from actual owned support and memory cards and currently available rentals, with locks and exclusions.
- Current model, progress, stop reasons and an uncalibrated exam score estimate.
- Integrated game launcher, optional translation, control-module installation and GUI updates.

The model covers N.I.A. Pro and Master. RL_v1 has completed training and independent data evaluation, but has not demonstrated consistent improvement over RL_v0. Some match scores are lower, and drink strategy still needs work. Secondary-choice data remains sparse in some scopes. Wins and high scores are not guaranteed. Weights stay fixed during a run; the app does not train or replace them automatically. BC selection is disabled.

A game-data version update alone does not block use. Supported cards and effects use the bundled reference definitions. If a decision needs an unlisted card, drink or effect, the app identifies the missing definition and stops. Actual game and reference-data versions are recorded separately. Unsupported game code or interfaces still stop; missing information is never replaced with zeros or rule-based decisions presented as model inference.

## Getting started

1. Download the full Windows package from [Releases](https://github.com/fullpie/gkms-assistant/releases) and extract it completely.
2. Start `GKMS-Assistant.exe`, select your game folder and install the required control components.
3. Select an idol, mode and run count, review the card setup, then start.

Requires Windows x64, Microsoft Edge WebView2 Runtime and .NET Framework 4.7.2 or later. The GUI runs in its own window with normal user privileges; game maintenance may request administrator permission. Keep the entire application folder together. Use `恢復 GUI.cmd` if a GUI update cannot start.

The public package includes fixed inference tensors, required static data and CPU dependencies; Python and an NVIDIA GPU are not required. It excludes training checkpoints, optimizer state, private research tools, account data, training replays, test DLLs and private native-simulation search. Loadout recommendations use the same model directly.

Original project portions are all rights reserved. Third-party licenses remain applicable. See [build documentation](docs/public-gui-packaging.md) for developer instructions.
