#include "EngineConfig.h"

#ifndef NOMINMAX
 #define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
 #define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

namespace gliss
{
namespace
{
// electron-builder's registry key of the Gliss installation: UUID v5 of the appId (io.github.tekalu1.gliss) in
// electron-builder's namespace. It does not change as long as the appId does not (app/tests/unit/release.spec.js checks it).
constexpr const wchar_t* installRegistryKey = L"Software\\a4620d0b-b9f5-551f-81ff-214a8d76afd2";

juce::File moduleFile()
{
    HMODULE module = nullptr;
    ::GetModuleHandleExW (GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                          reinterpret_cast<LPCWSTR> (&EngineConfig::discover), &module);
    wchar_t path[32768] {};
    if (module != nullptr && ::GetModuleFileNameW (module, path, (DWORD) std::size (path)) != 0)
        return juce::File (juce::String (path));
    return juce::File::getSpecialLocation (juce::File::currentExecutableFile);
}

juce::String env (const wchar_t* name)
{
    const auto size = ::GetEnvironmentVariableW (name, nullptr, 0);
    if (size == 0)
        return {};
    std::wstring value (size, L'\0');
    ::GetEnvironmentVariableW (name, value.data(), size);
    value.resize (size - 1);
    return juce::String (value.c_str());
}

EngineConfig fromMcpFile (const juce::File& file)
{
    EngineConfig config;
    if (! file.existsAsFile())
        return config;
    auto root = juce::JSON::parse (file);
    auto* servers = root.getProperty ("mcpServers", {}).getDynamicObject();
    if (servers == nullptr)
        return config;
    auto gliss = servers->getProperty ("gliss");
    auto command = gliss.getProperty ("command", {}).toString();
    if (command.isEmpty())
        return config;
    config.executable = juce::File (command);
    config.workingDirectory = juce::File (gliss.getProperty ("cwd", file.getParentDirectory().getChildFile ("engine").getFullPathName()).toString());
    if (auto* args = gliss.getProperty ("args", {}).getArray())
        for (const auto& arg : *args)
            config.arguments.add (arg.toString());
    if (auto* e = gliss.getProperty ("env", {}).getDynamicObject())
        for (const auto& property : e->getProperties())
            config.environment.set (property.name.toString(), property.value.toString());
    config.source = "mcp.json";
    return config;
}

juce::File repositoryEngineDirectory (const juce::File& module)
{
    auto directory = module.getParentDirectory();
    for (int depth = 0; depth < 10; ++depth)
    {
        auto engine = directory.getChildFile ("engine");
        if (engine.getChildFile ("vocal_engine").isDirectory())
            return engine;
        auto parent = directory.getParentDirectory();
        if (parent == directory) break;
        directory = parent;
    }
    return {};
}

/** The engine exe that belongs to an ancestor `directory` of the plug-in, if any. */
EngineConfig bundledEngine (const juce::File& directory)
{
    // <installDir>/resources: the copy of Gliss.vst3 inside a Gliss installation (resources/plugin/Gliss.vst3)
    // or the unpacked build (dist/win-unpacked/resources). Then a folder layout with engine/vocal-engine.exe.
    for (auto exe : { directory.getChildFile ("engine").getChildFile ("vocal-engine").getChildFile ("vocal-engine.exe"),
                      directory.getChildFile ("engine").getChildFile ("vocal-engine.exe") })
    {
        if (exe.existsAsFile())
        {
            EngineConfig config;
            config.executable = exe;
            config.workingDirectory = exe.getParentDirectory();
            config.source = "bundled";
            return config;
        }
    }
    return {};
}
} // namespace

EngineConfig EngineConfig::forInstallation (const juce::File& installDirectory)
{
    EngineConfig config;
    if (installDirectory == juce::File())
        return config;
    config.executable = installDirectory.getChildFile ("resources").getChildFile ("engine")
                                        .getChildFile ("vocal-engine").getChildFile ("vocal-engine.exe");
    config.workingDirectory = config.executable.getParentDirectory();
    return config;
}

juce::File EngineConfig::installDirectoryFromFile (const juce::File& installJson)
{
    if (! installJson.existsAsFile())
        return {};
    // The NSIS installer writes UTF-16LE with a BOM (it cannot write UTF-8); loadFileAsString reads both.
    const auto root = juce::JSON::parse (installJson.loadFileAsString());
    if (root.getProperty ("format", {}).toString() != "gliss-install")
        return {};
    const auto directory = root.getProperty ("installDir", {}).toString();
    if (! juce::File::isAbsolutePath (directory))
        return {};
    return juce::File (directory);
}

juce::File EngineConfig::installDirectoryFromRegistry()
{
    DWORD bytes = 0;
    if (::RegGetValueW (HKEY_CURRENT_USER, installRegistryKey, L"InstallLocation", RRF_RT_REG_SZ, nullptr, nullptr, &bytes) != ERROR_SUCCESS
        || bytes < sizeof (wchar_t))
        return {};
    std::wstring value (bytes / sizeof (wchar_t), L'\0');
    if (::RegGetValueW (HKEY_CURRENT_USER, installRegistryKey, L"InstallLocation", RRF_RT_REG_SZ, nullptr, value.data(), &bytes) != ERROR_SUCCESS)
        return {};
    const juce::String directory (value.c_str());
    return juce::File::isAbsolutePath (directory) ? juce::File (directory) : juce::File();
}

EngineConfig EngineConfig::discover()
{
    return discoverFor (moduleFile());
}

EngineConfig EngineConfig::discoverFor (const juce::File& module)
{
    EngineConfig config;
    const auto python = env (L"GLISS_ENGINE_PYTHON");
    const auto cwd = env (L"GLISS_ENGINE_CWD");
    if (python.isNotEmpty())
    {
        config.executable = juce::File (python);
        config.arguments.addArray ({ "-m", "vocal_engine.mcp" });
        config.workingDirectory = cwd.isNotEmpty() ? juce::File (cwd) : repositoryEngineDirectory (module);
        config.source = "env";
    }
    else
    {
        // Gliss.vst3/Contents/x86_64-win/Gliss.vst3 -> Gliss.vst3/Contents/Resources/gliss-install.json
        const auto installJson = module.getParentDirectory().getSiblingFile ("Resources").getChildFile ("gliss-install.json");
        config = forInstallation (installDirectoryFromFile (installJson));
        config.source = "install-file";

        auto directory = module.getParentDirectory();
        for (int depth = 0; depth < 10 && ! config.executable.existsAsFile(); ++depth)
        {
            config = bundledEngine (directory);
            if (config.executable.existsAsFile())
                break;
            config = fromMcpFile (directory.getChildFile (".mcp.json"));
            if (config.executable.existsAsFile())
                break;
            const auto parent = directory.getParentDirectory();
            if (parent == directory)
                break;
            directory = parent;
        }
        if (! config.executable.existsAsFile())
        {
            config = forInstallation (installDirectoryFromRegistry());
            config.source = "registry";
        }
        if (! config.executable.existsAsFile())
        {
            directory = juce::File::getCurrentWorkingDirectory();
            for (int depth = 0; depth < 10; ++depth)
            {
                config = fromMcpFile (directory.getChildFile (".mcp.json"));
                config.source = "cwd-mcp.json";
                if (config.executable.existsAsFile())
                    break;
                const auto parent = directory.getParentDirectory();
                if (parent == directory)
                    break;
                directory = parent;
            }
        }
        if (! config.executable.existsAsFile())
            config.source = {};
    }
    if (cwd.isNotEmpty())
        config.workingDirectory = juce::File (cwd);
    config.environment.set ("GLISS_CLIENT", "ara");
    config.environment.set ("PYTHONIOENCODING", "utf-8");
    return config;
}

} // namespace gliss
