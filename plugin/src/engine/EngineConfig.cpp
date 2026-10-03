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
    return config;
}

juce::File repositoryEngineDirectory()
{
    auto directory = moduleFile().getParentDirectory();
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
} // namespace

EngineConfig EngineConfig::discover()
{
    EngineConfig config;
    const auto python = env (L"GLISS_ENGINE_PYTHON");
    const auto cwd = env (L"GLISS_ENGINE_CWD");
    if (python.isNotEmpty())
    {
        config.executable = juce::File (python);
        config.arguments.addArray ({ "-m", "vocal_engine.mcp" });
        config.workingDirectory = cwd.isNotEmpty() ? juce::File (cwd) : repositoryEngineDirectory();
    }
    else
    {
        auto directory = moduleFile().getParentDirectory();
        for (int depth = 0; depth < 10; ++depth)
        {
            const auto bundled = directory.getChildFile ("engine").getChildFile ("vocal-engine.exe");
            if (bundled.existsAsFile())
            {
                config.executable = bundled;
                config.workingDirectory = bundled.getParentDirectory();
                break;
            }
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
            directory = juce::File::getCurrentWorkingDirectory();
            for (int depth = 0; depth < 10; ++depth)
            {
                config = fromMcpFile (directory.getChildFile (".mcp.json"));
                if (config.executable.existsAsFile())
                    break;
                const auto parent = directory.getParentDirectory();
                if (parent == directory)
                    break;
                directory = parent;
            }
        }
    }
    if (cwd.isNotEmpty())
        config.workingDirectory = juce::File (cwd);
    config.environment.set ("GLISS_CLIENT", "ara");
    config.environment.set ("PYTHONIOENCODING", "utf-8");
    return config;
}

} // namespace gliss
