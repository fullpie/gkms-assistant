# Development and packaging

[Back to the README](../README.md)

These technical notes were moved from the original 0.1.0 README to keep the project landing page focused on users.

## Public source scope

This is the allowlisted public GUI source export. It does not include account state, login credentials, research captures, game binaries or test DLLs.

Original project portions are all rights reserved; no additional open-source grant is made. Third-party licenses and source rights remain unchanged. See [NOTICE](../NOTICE.txt) and the license inventories.

The published application uses one existing GUI/controller owner. Two BC model choices share their original verified weights; selectable flows do not imply trained coverage or accepted policy quality.

## Building from this public repository

Install the pinned dependencies and build extra, then use:

```text
python tools/build_public_gui.py --workspace . --output BUILD --model-assets MODEL_ASSETS --model-manifest-sha256 SHA --loadout-assets LOADOUT_ASSETS --outer-assets OUTER_ASSETS --display-labels DISPLAY_LABELS --control-package CONTROL_ZIP --control-sha256 CONTROL_SHA
```

Qualified model, loadout, outer-rule/behavior assets and the control package are required. The exported native-source ledger is reused without private build receipts. Assets are separate release files, not checked into the source repository.

See [public GUI packaging notes](public-gui-packaging.md). Inspect the machine-readable build report; a candidate is not a published release.

## Windows runtime

The managed Windows layout has a normal-user launcher and immutable version slots. The interface opens in its own Windows WebView2 window, not in Chrome. Microsoft Edge WebView2 Evergreen Runtime and .NET Framework 4.7.2+ are required; the small presentation host is compiled from `native/gui_window/Program.cs` using the pinned Microsoft SDK.

User state stays in `LocalAppData/gkms-assistant`. GUI updates do not start or restart the game.
