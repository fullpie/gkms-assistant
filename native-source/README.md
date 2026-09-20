# Public native control corresponding source

The source closure follows the compiler inputs of the audited public bridge and normal action recorder. Only existing public preprocessor branches are selected; research profiles/probes are not included. The normal recorder is required for action settlement.

On Windows x64 with Visual Studio 2022 C++ tools, Windows SDK and Premake5 on PATH, run `premake5 vs2022`, then `msbuild build/projects/gkms_public_control.sln /m /p:Configuration=Release /p:Platform=x64`. This builds only bridge/recorder under build/bin and never installs or starts a game. Build the Localify loader separately using third_party/localify/README.md and its pinned Conan recipe.

Original project portions are all rights reserved. Third-party and derivative components retain their existing licenses; see third_party/localify/LICENSE-INVENTORY.json and retained inline notices. This source closure is not a claim of byte-identical reproduction or live workflow validation.
