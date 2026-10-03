// EngineConfig: how the distributed plug-in finds the engine of the installed Gliss (docs/ara-plugin.md "配布").
// The packaged checks run only when the environment names a build:
//   GLISS_TEST_PACKAGED_RESOURCES  <dist/win-unpacked>/resources (electron-builder --dir): the copy of Gliss.vst3 inside
//                                  the installation finds resources/engine and the engine answers engine_info
//   GLISS_TEST_INSTALLED_PLUGIN    Gliss.vst3/Contents/x86_64-win/Gliss.vst3 copied by the installer, next to the
//                                  gliss-install.json it wrote: found through that file, and the engine answers
#include "engine/EngineConfig.h"
#include "engine/McpClient.h"

#ifndef NOMINMAX
 #define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
 #define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

namespace
{
/** Clears a variable for the lifetime of the object (discoverFor reads GLISS_ENGINE_PYTHON / GLISS_ENGINE_CWD first). */
struct ScopedUnsetEnv
{
    explicit ScopedUnsetEnv (const wchar_t* n) : name (n)
    {
        wchar_t buffer[32768] {};
        const auto size = ::GetEnvironmentVariableW (name, buffer, (DWORD) std::size (buffer));
        had = size > 0;
        if (had) previous = buffer;
        ::SetEnvironmentVariableW (name, nullptr);
    }
    ~ScopedUnsetEnv() { ::SetEnvironmentVariableW (name, had ? previous.c_str() : nullptr); }
    const wchar_t* name;
    bool had = false;
    std::wstring previous;
};

/** The installer's gliss-install.json: UTF-16LE with a BOM (NSIS FileWriteUTF16LE /BOM). */
void writeInstallJson (const juce::File& file, const juce::String& installDir)
{
    const auto json = "{\"format\":\"gliss-install\",\"version\":1,\"installDir\":\""
                      + installDir.replace ("\\", "\\\\") + "\",\"appVersion\":\"0.0.0-test\"}\r\n";
    juce::MemoryOutputStream out;
    out.write ("\xff\xfe", 2);
    for (auto c : json)
        out.writeShort ((short) c);
    file.getParentDirectory().createDirectory();
    file.replaceWithData (out.getData(), out.getDataSize());
}

juce::File touch (const juce::File& file)
{
    file.getParentDirectory().createDirectory();
    file.replaceWithText ("x");
    return file;
}

juce::var noArgs() { return juce::var (new juce::DynamicObject()); }

class EngineConfigTests final : public juce::UnitTest
{
public:
    EngineConfigTests() : juce::UnitTest ("Engine discovery (distribution)", "Gliss") {}

    void runTest() override
    {
        struct ConsoleLogger final : juce::Logger
        {
            void logMessage (const juce::String& message) override { std::printf ("%s\n", message.toRawUTF8()); }
        } logger;
        auto* previousLogger = juce::Logger::getCurrentLogger();
        juce::Logger::setCurrentLogger (&logger);
        struct RestoreLogger
        {
            juce::Logger* previous;
            ~RestoreLogger() { juce::Logger::setCurrentLogger (previous); }
        } restore { previousLogger };
        ScopedUnsetEnv python (L"GLISS_ENGINE_PYTHON"), cwd (L"GLISS_ENGINE_CWD");
        auto root = juce::File::getSpecialLocation (juce::File::tempDirectory)
                        .getNonexistentChildFile ("GlissEngineConfigTest", {}, true);
        // a user folder with a non-ASCII name and a space (Japanese Windows user names are common)
        const auto installDir = root.getChildFile (juce::CharPointer_UTF8 ("\xe5\x88\xa9\xe7\x94\xa8\xe8\x80\x85 A"))
                                    .getChildFile ("Programs").getChildFile ("Gliss");
        const auto engineExe = touch (installDir.getChildFile ("resources/engine/vocal-engine/vocal-engine.exe"));

        beginTest ("gliss-install.json (UTF-16LE with BOM, UTF-8) names the installation");
        const auto json = root.getChildFile ("utf16.json");
        writeInstallJson (json, installDir.getFullPathName());
        expectEquals (gliss::EngineConfig::installDirectoryFromFile (json).getFullPathName(), installDir.getFullPathName());
        const auto utf8 = root.getChildFile ("utf8.json");
        utf8.replaceWithText ("{\"format\":\"gliss-install\",\"installDir\":\"" + installDir.getFullPathName().replace ("\\", "\\\\") + "\"}");
        expectEquals (gliss::EngineConfig::installDirectoryFromFile (utf8).getFullPathName(), installDir.getFullPathName());
        utf8.replaceWithText ("{\"format\":\"other\",\"installDir\":\"C:\\\\x\"}");
        expect (gliss::EngineConfig::installDirectoryFromFile (utf8) == juce::File());
        utf8.replaceWithText ("{\"format\":\"gliss-install\",\"installDir\":\"relative\\\\x\"}");
        expect (gliss::EngineConfig::installDirectoryFromFile (utf8) == juce::File());
        expect (gliss::EngineConfig::installDirectoryFromFile (root.getChildFile ("missing.json")) == juce::File());

        beginTest ("The plug-in in the VST3 folder finds the engine through gliss-install.json");
        const auto bundle = root.getChildFile ("VST3/Gliss.vst3");
        const auto module = touch (bundle.getChildFile ("Contents/x86_64-win/Gliss.vst3"));
        writeInstallJson (bundle.getChildFile ("Contents/Resources/gliss-install.json"), installDir.getFullPathName());
        auto found = gliss::EngineConfig::discoverFor (module);
        expectEquals (found.executable.getFullPathName(), engineExe.getFullPathName());
        expectEquals (found.workingDirectory.getFullPathName(), engineExe.getParentDirectory().getFullPathName());
        expectEquals (found.source, juce::String ("install-file"));
        expect (found.arguments.isEmpty());
        expectEquals (found.environment["GLISS_CLIENT"], juce::String ("ara"));

        beginTest ("The copy inside the installation finds resources/engine");
        const auto inside = touch (installDir.getChildFile ("resources/plugin/Gliss.vst3/Contents/x86_64-win/Gliss.vst3"));
        found = gliss::EngineConfig::discoverFor (inside);
        expectEquals (found.executable.getFullPathName(), engineExe.getFullPathName());
        expectEquals (found.source, juce::String ("bundled"));

        beginTest ("A gliss-install.json that points to a removed installation is not used");
        writeInstallJson (bundle.getChildFile ("Contents/Resources/gliss-install.json"), root.getChildFile ("gone").getFullPathName());
        found = gliss::EngineConfig::discoverFor (module);
        expect (found.source != "install-file");
        expect (! found.executable.isAChildOf (root.getChildFile ("gone")));

        root.deleteRecursively();

        const auto resources = juce::SystemStats::getEnvironmentVariable ("GLISS_TEST_PACKAGED_RESOURCES", {});
        if (resources.isNotEmpty())
        {
            beginTest ("Packaged: the copy inside the unpacked build starts the bundled engine");
            const auto dir = juce::File (resources);
            const auto plugin = dir.getChildFile ("plugin/Gliss.vst3/Contents/x86_64-win/Gliss.vst3");
            expect (plugin.existsAsFile(), "missing " + plugin.getFullPathName());
            const auto config = gliss::EngineConfig::discoverFor (plugin);
            expectEquals (config.executable.getFullPathName(),
                          dir.getChildFile ("engine/vocal-engine/vocal-engine.exe").getFullPathName());
            expectEquals (config.source, juce::String ("bundled"));
            expectEngineAnswers (config);
        }

        const auto installed = juce::SystemStats::getEnvironmentVariable ("GLISS_TEST_INSTALLED_PLUGIN", {});
        if (installed.isNotEmpty())
        {
            beginTest ("Installed: the plug-in in the VST3 folder starts the engine of the installation");
            const auto config = gliss::EngineConfig::discoverFor (juce::File (installed));
            expectEquals (config.source, juce::String ("install-file"));
            expectEngineAnswers (config);
        }
    }

    /** Starts the engine (no model weights needed) and asks engine_info, with the work and log folders in a temp folder. */
    void expectEngineAnswers (gliss::EngineConfig config)
    {
        expect (config.isValid(), "not runnable: " + config.executable.getFullPathName());
        if (! config.isValid())
            return;
        auto temp = juce::File::getSpecialLocation (juce::File::tempDirectory).getNonexistentChildFile ("GlissEngineRun", {}, true);
        temp.getChildFile ("LocalAppData").createDirectory();
        config.environment.set ("LOCALAPPDATA", temp.getChildFile ("LocalAppData").getFullPathName());
        config.environment.set ("VOCAL_ENGINE_WORK_DIR", temp.getChildFile ("work").getFullPathName());
        config.environment.set ("VOCAL_ENGINE_LOG_DIR", temp.getChildFile ("log").getFullPathName());
        {
            gliss::McpClient engine (config);
            const auto info = engine.call ("engine_info", noArgs(), 120000);
            logMessage ("engine_info: " + juce::JSON::toString (info, true).substring (0, 300));
            expect ((bool) info.getProperty ("ok", false) || info.hasProperty ("version"), "engine_info failed");
            const auto pid = engine.processId();
            HANDLE process = ::OpenProcess (SYNCHRONIZE, FALSE, (DWORD) pid);
            engine.stop();
            if (process != nullptr)
            {
                expect (::WaitForSingleObject (process, 5000) == WAIT_OBJECT_0, "engine still running");
                ::CloseHandle (process);
            }
        }
        temp.deleteRecursively();
    }
};

EngineConfigTests engineConfigTests;
} // namespace
