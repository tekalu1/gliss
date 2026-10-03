#include "engine/McpClient.h"
#include <juce_audio_formats/juce_audio_formats.h>
#include <atomic>
#include <cmath>
#include <thread>

namespace
{
juce::var args (std::initializer_list<std::pair<juce::Identifier, juce::var>> fields)
{
    auto* o = new juce::DynamicObject();
    for (const auto& [key, value] : fields) o->setProperty (key, value);
    return juce::var (o);
}

juce::File pythonExe()
{
    const auto configured = juce::SystemStats::getEnvironmentVariable ("GLISS_ENGINE_PYTHON", {});
    if (configured.isNotEmpty()) return juce::File (configured);
    wchar_t path[32768] {};
    if (::SearchPathW (nullptr, L"python.exe", nullptr, (DWORD) std::size (path), path, nullptr) != 0)
        return juce::File (juce::String (path));
    return {};
}

class McpClientTests final : public juce::UnitTest
{
public:
    McpClientTests() : juce::UnitTest ("MCP engine process", "Gliss") {}

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
        const auto python = pythonExe();
        beginTest ("Python is available for the fake MCP engine");
        expect (python.existsAsFile());
        if (! python.existsAsFile()) return;

        auto directory = juce::File::getSpecialLocation (juce::File::tempDirectory)
                             .getNonexistentChildFile ("GlissMcpTest", {}, true);
        expect (directory.createDirectory());
        const auto script = directory.getChildFile ("fake_engine.py");
        const auto childPid = directory.getChildFile ("child.pid");
        const char* code = R"PY(import json, os, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
with open(os.environ['GLISS_TEST_CHILD_PID_PATH'], 'w') as f:
    f.write(str(child.pid))
queued = []
def send(id, value):
    print(json.dumps({'jsonrpc':'2.0','id':id,'result':{'structuredContent':{'result':value}}}), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if 'id' not in msg:
        continue
    if msg['method'] == 'initialize':
        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'protocolVersion':'2025-03-26','capabilities':{},'serverInfo':{'name':'fake','version':'1'}}}), flush=True)
        continue
    name = msg['params']['name']
    if name == 'pair':
        queued.append(msg)
        if len(queued) == 2:
            for call in reversed(queued):
                send(call['id'], {'ok':True,'value':call['params']['arguments']['value'],'client':os.getenv('GLISS_CLIENT')})
            queued = []
    elif name == 'crash':
        os._exit(7)
    elif name == 'slow':
        time.sleep(0.5)
        send(msg['id'], {'ok':True})
    elif name == 'fail':
        send(msg['id'], {'ok':False,'error':'expected failure'})
    else:
        send(msg['id'], {'ok':True,'name':name})
)PY";
        expect (script.replaceWithText (code));
        gliss::EngineConfig fake;
        fake.executable = python;
        fake.arguments.add (script.getFullPathName());
        fake.workingDirectory = directory;
        fake.environment.set ("GLISS_TEST_CHILD_PID_PATH", childPid.getFullPathName());
        fake.environment.set ("GLISS_CLIENT", "ara");
        fake.environment.set ("PYTHONIOENCODING", "utf-8");

        beginTest ("Concurrent requests match reversed response IDs");
        gliss::McpClient client (fake);
        juce::var first, second;
        std::thread a ([&] { first = client.call ("pair", args ({ { "value", 11 } }), 3000); });
        std::thread b ([&] { second = client.call ("pair", args ({ { "value", 22 } }), 3000); });
        a.join(); b.join();
        expectEquals ((int) first.getProperty ("value", {}), 11);
        expectEquals ((int) second.getProperty ("value", {}), 22);
        expectEquals (first.getProperty ("client", {}).toString(), juce::String ("ara"));

        beginTest ("Tool ok:false is returned as data");
        auto failed = client.call ("fail");
        expect (! (bool) failed.getProperty ("ok", true));
        expectEquals (failed.getProperty ("error", {}).toString(), juce::String ("expected failure"));

        beginTest ("Timeout does not pair a late response with the next request");
        auto timeout = client.call ("slow", args ({}), 50);
        expectEquals (timeout.getProperty ("error", {}).toString(), juce::String ("Engine response timed out"));
        expectEquals (client.call ("after-timeout", args ({}), 3000).getProperty ("name", {}).toString(),
                      juce::String ("after-timeout"));

        beginTest ("Exit callback fires and the next call starts a new engine");
        std::atomic<int> exits { 0 };
        client.setExitCallback ([&] { ++exits; });
        auto crashed = client.call ("crash", args ({}), 3000);
        expectEquals (crashed.getProperty ("error", {}).toString(), juce::String ("Engine process exited"));
        for (int retry = 0; retry < 100 && exits == 0; ++retry) juce::Thread::sleep (10);
        expectEquals (exits.load(), 1);
        expectEquals (client.call ("restarted").getProperty ("name", {}).toString(), juce::String ("restarted"));

        beginTest ("Closing the job kills the fake engine's child");
        const int pid = childPid.loadFileAsString().trim().getIntValue();
        expect (pid > 0);
        HANDLE child = ::OpenProcess (SYNCHRONIZE, FALSE, (DWORD) pid);
        expect (child != nullptr);
        client.stop();
        if (child != nullptr)
        {
            expect (::WaitForSingleObject (child, 3000) == WAIT_OBJECT_0);
            ::CloseHandle (child);
        }

        const auto realPython = juce::SystemStats::getEnvironmentVariable ("GLISS_ENGINE_PYTHON", {});
        const auto realCwd = juce::SystemStats::getEnvironmentVariable ("GLISS_ENGINE_CWD", {});
        if (realPython.isNotEmpty() && realCwd.isNotEmpty())
        {
            beginTest ("Real ARA engine: info, open, modification, render");
            const auto work = directory.getChildFile ("work");
            work.createDirectory();
            const auto wav = directory.getChildFile ("synthetic.wav");
            juce::AudioBuffer<float> samples (1, 44100);
            for (int i = 0; i < samples.getNumSamples(); ++i)
                samples.setSample (0, i, (float) (0.2 * std::sin (juce::MathConstants<double>::twoPi * 220.0 * i / 44100.0)));
            juce::WavAudioFormat format;
            if (auto stream = std::unique_ptr<juce::OutputStream> (wav.createOutputStream()))
                if (auto writer = format.createWriterFor (stream, juce::AudioFormatWriterOptions()
                                                              .withSampleRate (44100.0)
                                                              .withNumChannels (1)
                                                              .withBitsPerSample (16)))
                {
                    writer->writeFromAudioSampleBuffer (samples, 0, samples.getNumSamples());
                }
            gliss::EngineConfig real;
            real.executable = juce::File (realPython);
            real.workingDirectory = juce::File (realCwd);
            real.arguments.addArray ({ "-m", "vocal_engine.mcp" });
            real.environment.set ("GLISS_CLIENT", "ara");
            real.environment.set ("PYTHONIOENCODING", "utf-8");
            real.environment.set ("VOCAL_ENGINE_WORK_DIR", work.getFullPathName());
            real.environment.set ("GLISS_F0_ESTIMATOR", "praat");
            gliss::McpClient engine (real);
            expect ((bool) engine.call ("engine_info").getProperty ("ok", false));
            expect ((bool) engine.call ("ara_open", args ({ { "work_key", "c1_synthetic" } })).getProperty ("ok", false));
            expect ((bool) engine.call ("ara_set_modification", args ({ { "ara_id", "m1" }, { "source_path", wav.getFullPathName() } }), 30000)
                             .getProperty ("ok", false));
            expect ((bool) engine.call ("ara_render_dirty", args ({ { "ara_id", "m1" } }), 30000)
                             .getProperty ("ok", false));
            HANDLE realProcess = ::OpenProcess (SYNCHRONIZE, FALSE, (DWORD) engine.processId());
            expect (realProcess != nullptr);
            engine.stop();
            if (realProcess != nullptr)
            {
                expect (::WaitForSingleObject (realProcess, 3000) == WAIT_OBJECT_0);
                ::CloseHandle (realProcess);
            }
        }
        directory.deleteRecursively();
    }
};

McpClientTests mcpClientTests;
} // namespace
