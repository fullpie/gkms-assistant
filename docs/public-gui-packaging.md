# GUI 0.1 public package

The public package uses the existing GUI/controller owner with `public_build=True`.
It does not contain the developer addon, raw account/replay data, historical
research directories, test DLLs, or standalone research entrypoints. Required
legacy Python modules remain in the conservative import closure and are listed
in `public-source-manifest.json`; their presence is not a public feature switch.

Original project material is supplied with all rights reserved, following the
owner's explicit instruction. Existing third-party and derivative licenses
remain applicable. Native corresponding source includes the Localify source
archive and the compiled bridge/normal-recorder source closure. The ordinary
action recorder is required by runtime settlement and is not a research probe.

## Build and export

Use a new output directory for every candidate. Source-only export does not
claim executable, model, installation, or live validation:

```powershell
.venv/Scripts/python.exe -X utf8 scripts/build_public_gui.py `
  --output BUILD_SOURCE --source-only `
  --native-source-receipt NATIVE_VALIDATION_RECEIPT
```

Build the Windows onedir GUI and bootstrap using PyInstaller 6.22.3:

```powershell
.venv/Scripts/python.exe -X utf8 scripts/build_public_gui.py `
  --output BUILD_CANDIDATE `
  --model-assets PORTABLE_MODEL_DIRECTORY `
  --model-manifest-sha256 MODEL_MANIFEST_SHA256 `
  --loadout-assets PORTABLE_LOADOUT_DIRECTORY `
  --outer-assets PORTABLE_OUTER_DIRECTORY `
  --display-labels DISPLAY_LABEL_DIRECTORY `
  --control-package CONTROL_ZIP --control-sha256 CONTROL_ZIP_SHA256 `
  --native-source-receipt NATIVE_VALIDATION_RECEIPT
```

The model package is release-pinned. Loadout and outer rule data are independently
pinned to the same current Master source; historical behavior provenance remains
separate. The build copies only manifest-listed assets, checks the
original numerical source bytes, preserves tensor identities, and loads both
actual policy factories in the compiled executable. It does not train, select,
activate, or execute a policy. Missing prerequisites appear as machine-readable
blockers in `build-report.json`.

Build outputs include:

- `gkms-assistant-0.1.0-windows-x64.zip`: first-install portable application.
- `gkms-assistant-gui-0.1.0-windows-x64.zip`: immutable GUI version slot.
- `gkms-assistant-gui-release.json`: GUI updater identity and archive digest.
- `gkms-assistant-source-0.1.0.zip`: allowlisted corresponding application source.
- Build logs, source/asset/license manifests, and offline self-check receipts.

The source exporter does not silently rewrite private paths or omit required
imports. Such paths are blockers. The native exporter records its narrowly
selected public compilation branches and source hashes; source export alone
does not prove a fresh native rebuild or game compatibility.

## Startup and update boundary

Users start `GKMS-Assistant.exe` with ordinary Windows privileges. It resolves
the verified active version slot and starts its `gkms-assistant.exe`. Application
data defaults to `%LOCALAPPDATA%/gkms-assistant`, outside immutable slots. Only the
fixed, separately guarded maintenance-worker role may request elevated work.

Each ordinary GUI process checks stable GUI releases from
`fullpie/gkms-assistant` in the background. Translation releases use a separate
source and identity. A newer release is downloaded only on user request. Its
archive, manifest, file inventory, size bounds, paths and hashes are verified
before staging. Applying or rolling back changes the managed pointer after the
existing owner rechecks that no cultivation/native transaction is pending. It
retains the previous slot and does not restart the game or GUI automatically.
Development checkouts do not overwrite themselves.

An interrupted or uncertain operation retains its original operation ID. The
frontend observes that operation's terminal receipt; a missing HTTP response
does not cause a second mutation request.

## Isolated GUI acceptance

After the offline build succeeds, the following explicitly starts a separate
read-only GUI with empty user and native-state directories:

```powershell
.venv/Scripts/python.exe -X utf8 scripts/check_public_gui_candidate.py `
  --managed-root BUILD_CANDIDATE/managed --output NEW_DIAGNOSTIC_DIRECTORY
```

The diagnostic checks the actual Tk-owned HTTP snapshot, public assets and
developer-area exclusion, then sends shutdown only to the process it started.
The private loopback token is never printed. It does not start a game, request
UAC, inspect an existing game session, install files, or submit a game action.
`--session-file` is accepted by the public entry only with read-only/no-browser
options and only inside that process's user-data directory.

`candidate_complete`, source export, offline policy loading, real GUI startup,
native installation, live workflow, policy wins/score quality and publication
are separate results. The build always leaves `release_ready=false` pending the
owner's final review; it never publishes automatically.
