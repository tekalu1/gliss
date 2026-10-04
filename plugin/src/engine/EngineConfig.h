#pragma once

#include <juce_core/juce_core.h>

namespace gliss
{

/** One stdio MCP engine invocation. Paths may be overridden for tests. */
struct EngineConfig
{
    juce::File executable;
    juce::StringArray arguments;
    juce::File workingDirectory;
    juce::StringPairArray environment;
    /** How the engine was found (for logs): "env", "install-file", "bundled", "mcp.json", "registry", "cwd-mcp.json". */
    juce::String source;

    bool isValid() const { return executable.existsAsFile() && workingDirectory.isDirectory(); }

    /** Finds development settings or the engine of the installed Gliss (see discoverFor). */
    static EngineConfig discover();

    /** discover() as if the plug-in binary were `module` (Gliss.vst3/Contents/x86_64-win/Gliss.vst3). In this order:
        1. GLISS_ENGINE_PYTHON (+ GLISS_ENGINE_CWD): development, `python -m vocal_engine.mcp`.
        2. Gliss.vst3/Contents/Resources/gliss-install.json, written by the installer: the Gliss installation folder.
        3. Ancestors of the module: an installation's `resources` folder (the copy inside the installation),
           `engine/vocal-engine.exe`, or a development `.mcp.json`.
        4. The installation folder in the registry (HKCU, written by the installer).
        5. `.mcp.json` in the ancestors of the current directory. */
    static EngineConfig discoverFor (const juce::File& module);

    /** The engine shipped with a Gliss installation: <installDir>/resources/engine/vocal-engine/vocal-engine.exe. */
    static EngineConfig forInstallation (const juce::File& installDirectory);

    /** The installation folder named by gliss-install.json (UTF-8 or UTF-16 with BOM); empty if missing or invalid. */
    static juce::File installDirectoryFromFile (const juce::File& installJson);

    /** The installation folder from HKCU\Software\<electron-builder GUID of io.github.tekalu1.gliss>\InstallLocation. */
    static juce::File installDirectoryFromRegistry();
};

} // namespace gliss
