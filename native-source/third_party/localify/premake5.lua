dependencies = {
	basePath = "./deps"
}

function dependencies.load()
	dir = path.join(dependencies.basePath, "premake/*.lua")
	deps = os.matchfiles(dir)

	for i, dep in pairs(deps) do
		dep = dep:gsub(".lua", "")
		require(dep)
	end
end

function dependencies.imports()
	for i, proj in pairs(dependencies) do
		if type(i) == 'number' then
			proj.import()
		end
	end
end

function dependencies.projects()
	for i, proj in pairs(dependencies) do
		if type(i) == 'number' then
			proj.project()
		end
	end
end

include "deps/minhook.lua"
include "deps/rapidjson.lua"
include "build/conanbuildinfo.premake.lua"

workspace "gakumas_localify_dmm"
	conan_basic_setup()

	location "./build"
	objdir "%{wks.location}/obj"
	targetdir "%{wks.location}/bin/%{cfg.platform}/%{cfg.buildcfg}"

	architecture "x64"
	platforms "x64"

	configurations {
		"Debug",
		"Release",
	}

	buildoptions {
		"/std:c++latest",
		"/utf-8",
	}
	systemversion "latest"
	symbols "On"
	staticruntime "On"
	editandcontinue "Off"
	warnings "Off"
	characterset "ASCII"

	flags {
		"NoIncrementalLink",
		"NoMinimalRebuild",
		"MultiProcessorCompile",
	}

	staticruntime "Off"

	configuration "Release"
		optimize "Full"
		buildoptions "/Os"

	configuration "Debug"
		optimize "Debug"

	dependencies.projects()


    project "gkms_public_runtime_loader"
        targetname "version"
        targetdir "build/bin/%{cfg.platform}/%{cfg.buildcfg}"
        objdir "build/public_obj/%{cfg.platform}/%{cfg.buildcfg}"
        language "C++"
        kind "SharedLib"
        files { "src/**.hpp", "src/**.cpp", "src/**.asm", "src/**.def" }
        removefiles { "src/GakumasLocalify/HeadlessReplayState*.cpp", "src/GakumasLocalify/Leaderboard*Dispatcher.cpp", "src/GakumasLocalify/LeaderboardQueryQueue.cpp" }
        includedirs { "src", "%{prj.location}/src" }
        defines { "GKMS_RUNTIME_COMMAND_BRIDGE", "GKMS_PUBLIC_PORTABLE" }
        buildoptions { "/experimental:deterministic",
            '/pathmap:"' .. path.getabsolute(".") .. '=public-loader"',
            '/pathmap:"' .. os.getenv("USERPROFILE") .. '=build-user"' }
        dependencies.imports()
        linkoptions { conan_sharedlinkflags, "/IGNORE:4099", "/PDBALTPATH:version.pdb" }
        configuration "Release"
            linkoptions "/SAFESEH:NO"
            syslibdirs { "libs/Release" }
        configuration {}
