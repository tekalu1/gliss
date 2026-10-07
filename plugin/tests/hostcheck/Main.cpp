// GlissHostCheck: JUCE のホストとして Gliss.vst3 を読み込み、画面を開かずに（実際は画面の外に置いて）確かめる。
//
//   GlissHostCheck <結果を書くファイル> <Gliss.vst3 のバイナリ> [<GLISS_ARA_TRACE_DIR と同じフォルダ、または ->] [--no-editor]
//   GlissHostCheck --editor <結果を書くファイル> [--expect-web-dir] [--cycles <回数>]
//   GlissHostCheck --ara-editor <結果を書くファイル> <Gliss.vst3 のバイナリ> <GLISS_ARA_TRACE_DIR と同じフォルダ> [--timeout <秒>]
//   GlissHostCheck --ara-playback <結果を書くファイル> <Gliss.vst3 のバイナリ> <GLISS_PLUGIN_LOG_FILE と同じファイル> [--timeout <秒>]
//   GlissHostCheck --ara-preview <結果を書くファイル> <Gliss.vst3 のバイナリ> <GLISS_TEST_BRIDGE_DIR と同じフォルダ> <GLISS_ARA_TRACE_DIR と同じフォルダ>
//
// 確かめること:
//   1. VST3 として見つかり、PluginDescription が ARA の拡張を持つと言う
//   2. ARA ファクトリが取れ、ID が決めたとおりである
//   3. インスタンスを作り、ARA に結び付かない（普通の VST3 の）ブロック処理が入力を変えない
//   4. エディタを作り、画面の外の窓に置く。ARA に結び付けていないので、エディタは画面（WebView2）を出さずに案内を出す
//      （プラグインが GLISS_ARA_TRACE_DIR に "editor: not bound to an ARA document" を書く。第 3 引数を渡したときだけ）
// --editor は Gliss.vst3 を読まず、エディタの画面の部品（plugin/src/editor）を偽の DocumentBridge につないでこのプロセスの中で
// 確かめる（EditorCheck.h の冒頭）。
// --ara-editor は JUCE の ARA ホストで Gliss.vst3 に本物のドキュメントを作り、エディタの役で結び付けて画面を開き、
// 本物のエンジンまで engineCall が通ることを確かめる（AraEditorCheck.h の冒頭）。
// --ara-playback は同じく ARA のドキュメントを作り、再生の役で準備した後に起きる変化（サンプルへのアクセスの切り替え・内容の更新・リージョンの追加）の
// あとも原音が鳴り続けることを確かめる（AraPlaybackCheck.h の冒頭。エンジンは GLISS_ENGINE_DISABLED=1 で止めて呼ぶ）。
// --ara-preview は 2 つのインスタンスを同じドキュメントに結び付け、試聴を求めたインスタンスの EditorRenderer だけが試聴の音を足すことを確かめる
// （AraPreviewCheck.h の冒頭。GLISS_TEST_HOOKS のビルドと GLISS_TEST_BRIDGE_DIR が要る）。
// 窓は画面の外に置き、SW_SHOWNA（前面にも入力の対象にもならない）で出す。結果は 0（全部通った）か 1 で返す。
#include "AraEditorCheck.h"
#include "AraPlaybackCheck.h"
#include "AraPreviewCheck.h"
#include "EditorCheck.h"

#include <juce_audio_processors/juce_audio_processors.h>
#include <juce_gui_extra/juce_gui_extra.h>

namespace
{
constexpr auto expectedFactoryID = "io.github.tekalu1.gliss.arafactory.3";
constexpr auto expectedArchiveID = "io.github.tekalu1.gliss.aradocumentarchive.1";

/** エディタを画面の外に置く窓。 */
class OffscreenWindow final : public juce::Component
{
public:
    explicit OffscreenWindow (juce::AudioProcessorEditor& editorIn) : editor (editorIn)
    {
        addAndMakeVisible (editor);
        setBounds (-20000, -20000, editor.getWidth(), editor.getHeight());
        setWantsKeyboardFocus (false);
        addToDesktop (juce::ComponentPeer::windowIsTemporary);
        setVisible (true);
    }

    void resized() override { editor.setBounds (getLocalBounds()); }

private:
    juce::AudioProcessorEditor& editor;
};
}

class HostCheckApplication final : public juce::JUCEApplication,
                                   private juce::Timer
{
public:
    const juce::String getApplicationName() override { return "GlissHostCheck"; }
    const juce::String getApplicationVersion() override { return "1.0.0"; }
    bool moreThanOneInstanceAllowed() override { return true; }

    void initialise (const juce::String&) override
    {
        const auto args = getCommandLineParameterArray();

        if (args.size() >= 2 && args[0] == "--editor")
            return runEditorCheck (args);

        if (args.size() >= 5 && args[0] == "--ara-preview")
            return runAraPreviewCheck (args);

        if (args.size() >= 4 && args[0] == "--ara-playback")
            return runAraPlaybackCheck (args);

        if (args.size() >= 4 && (args[0] == "--ara-editor" || args[0] == "--ara-audition"))
            return runAraEditorCheck (args);

        if (args.size() < 2)
        {
            std::fprintf (stderr, "usage: GlissHostCheck <report file> <Gliss.vst3> [<trace dir> or -] [--no-editor]\n"
                                  "       GlissHostCheck --editor <report file> [--expect-web-dir] [--cycles <n>]\n"
                                  "       GlissHostCheck --ara-editor <report file> <Gliss.vst3> <trace dir> [--timeout <s>]\n"
                                  "       GlissHostCheck --ara-playback <report file> <Gliss.vst3> <plugin log file> [--timeout <s>]\n"
                                  "       GlissHostCheck --ara-preview <report file> <Gliss.vst3> <test bridge dir> <trace dir>\n");
            setApplicationReturnValue (2);
            quit();
            return;
        }

        reportFile = juce::File (args[0]);
        pluginPath = args[1];

        if (args.size() > 2 && args[2] != "-")
            traceDir = juce::File (args[2]);

        skipEditor = args.contains ("--no-editor");

        reportFile.deleteFile();
        formatManager.addFormat (std::make_unique<juce::VST3PluginFormat>());

        juce::OwnedArray<juce::PluginDescription> descriptions;
        formatManager.getFormat (0)->findAllTypesForFile (descriptions, pluginPath);

        if (! check (descriptions.size() == 1, "found exactly one VST3 class (got " + juce::String (descriptions.size()) + ")"))
            return finish();

        description = *descriptions[0];
        report ("name=" + description.name + " manufacturer=" + description.manufacturerName + " version=" + description.version);
        check (description.hasARAExtension, "PluginDescription.hasARAExtension");

        createInstance();
    }

    void shutdown() override
    {
        stopTimer();
        editorCheck.reset();
        araEditorCheck.reset();
        window.reset();
        editor.reset();
        araFactory = {};
        instance.reset();
    }

private:
    void runEditorCheck (const juce::StringArray& args)
    {
        reportFile = juce::File (args[1]);
        reportFile.deleteFile();

        const auto cyclesIndex = args.indexOf ("--cycles");
        const auto cycles = cyclesIndex >= 0 ? args[cyclesIndex + 1].getIntValue() : 20;

        editorCheck = std::make_unique<EditorCheck> ([this] (const juce::String& line) { report (line); },
                                                     [this] (bool ok, const juce::String& what) { return check (ok, what); },
                                                     args.contains ("--expect-web-dir"), cycles);
        editorCheck->start ([this]
        {
            report (failures == 0 ? "RESULT OK" : "RESULT FAILED (" + juce::String (failures) + ")");
            setApplicationReturnValue (failures == 0 ? 0 : 1);

            // WebView2 の後始末はメッセージループの上で非同期に進むので、少し回してから終わる
            juce::Timer::callAfterDelay (teardownWaitMs, [this]
            {
                editorCheck.reset();
                report ("teardown: quit");
                quit();
            });
        });
    }

    void runAraEditorCheck (const juce::StringArray& args)
    {
        reportFile = juce::File (args[1]);
        reportFile.deleteFile();

        const auto timeoutIndex = args.indexOf ("--timeout");
        const auto timeoutSec = timeoutIndex >= 0 ? args[timeoutIndex + 1].getIntValue() : 180;
        const auto modsIndex = args.indexOf ("--mods");
        const auto mods = modsIndex >= 0 ? args[modsIndex + 1].getIntValue() : 1;
        const auto voiceIndex = args.indexOf ("--voice-sec");
        const auto voiceSec = voiceIndex >= 0 ? args[voiceIndex + 1].getIntValue() : 6;
        const auto auditionsIndex = args.indexOf ("--auditions");
        const auto auditions = auditionsIndex >= 0 ? args[auditionsIndex + 1].getIntValue() : 1;

        araEditorCheck = std::make_unique<AraEditorCheck> ([this] (const juce::String& line) { report (line); },
                                                           [this] (bool ok, const juce::String& what) { return check (ok, what); },
                                                           juce::File (args[2]), juce::File (args[3]), timeoutSec,
                                                           args[0] == "--ara-audition", mods, voiceSec, auditions);
        araEditorCheck->start ([this]
        {
            report (failures == 0 ? "RESULT OK" : "RESULT FAILED (" + juce::String (failures) + ")");
            setApplicationReturnValue (failures == 0 ? 0 : 1);

            juce::Timer::callAfterDelay (teardownWaitMs, [this]
            {
                araEditorCheck.reset();
                report ("teardown: quit");
                quit();
            });
        });
    }

    void runAraPlaybackCheck (const juce::StringArray& args)
    {
        reportFile = juce::File (args[1]);
        reportFile.deleteFile();

        const auto timeoutIndex = args.indexOf ("--timeout");
        const auto timeoutSec = timeoutIndex >= 0 ? args[timeoutIndex + 1].getIntValue() : 4;

        araPlaybackCheck = std::make_unique<AraPlaybackCheck> ([this] (const juce::String& line) { report (line); },
                                                              [this] (bool ok, const juce::String& what) { return check (ok, what); },
                                                              juce::File (args[2]), juce::File (args[3]), timeoutSec);
        araPlaybackCheck->start ([this]
        {
            report (failures == 0 ? "RESULT OK" : "RESULT FAILED (" + juce::String (failures) + ")");
            setApplicationReturnValue (failures == 0 ? 0 : 1);

            juce::Timer::callAfterDelay (teardownWaitMs, [this]
            {
                araPlaybackCheck.reset();
                report ("teardown: quit");
                quit();
            });
        });
    }

    void runAraPreviewCheck (const juce::StringArray& args)
    {
        reportFile = juce::File (args[1]);
        reportFile.deleteFile();

        araPreviewCheck = std::make_unique<AraPreviewCheck> ([this] (const juce::String& line) { report (line); },
                                                            [this] (bool ok, const juce::String& what) { return check (ok, what); },
                                                            juce::File (args[2]), juce::File (args[3]), juce::File (args[4]));
        araPreviewCheck->start ([this]
        {
            report (failures == 0 ? "RESULT OK" : "RESULT FAILED (" + juce::String (failures) + ")");
            setApplicationReturnValue (failures == 0 ? 0 : 1);

            juce::Timer::callAfterDelay (teardownWaitMs, [this]
            {
                araPreviewCheck.reset();
                report ("teardown: quit");
                quit();
            });
        });
    }

    void createInstance()
    {
        formatManager.createPluginInstanceAsync (description, 48000.0, 512,
                                                 [this] (std::unique_ptr<juce::AudioPluginInstance> created, const juce::String& error)
        {
            if (! check (created != nullptr, "plugin instance created " + error))
                return finish();

            instance = std::move (created);
            checkPassthrough();

            // ファクトリはインスタンスを通して取る（AudioPluginHost と同じ）。AudioPluginFormatManager から DLL の
            // ハンドルを持たずに取ると、先に DLL が外れて、ファクトリを手放すときに外れた DLL の中を呼んで落ちる
            // （JUCE の ARAPluginDemo でも同じに落ちる）。
            juce::createARAFactoryAsync (*instance, [this] (juce::ARAFactoryWrapper wrapper)
            {
                checkARAFactory (wrapper);
                araFactory = std::move (wrapper);

                if (skipEditor)
                    return finish();

                createEditor();
            });
        });
    }

    void checkARAFactory (const juce::ARAFactoryWrapper& wrapper)
    {
        const auto* factory = wrapper.get();

        if (! check (factory != nullptr, "ARA factory created"))
            return;

        check (juce::String (factory->factoryID) == expectedFactoryID, "factoryID = " + juce::String (factory->factoryID));
        check (juce::String (factory->documentArchiveID) == expectedArchiveID, "documentArchiveID = " + juce::String (factory->documentArchiveID));
        check (factory->analyzeableContentTypesCount == 1 && factory->analyzeableContentTypes[0] == ARA::kARAContentTypeNotes,
               "analyzeableContentTypes = notes only (count " + juce::String ((int) factory->analyzeableContentTypesCount) + ")");
        report ("plugInName=" + juce::String (factory->plugInName) + " apiGeneration=" + juce::String ((int) factory->lowestSupportedApiGeneration)
                + ".." + juce::String ((int) factory->highestSupportedApiGeneration));
    }

    /** ARA に結び付けずに processBlock を呼ぶと、入力がそのまま出る（普通の VST3 としての素通し）。 */
    void checkPassthrough()
    {
        instance->setPlayConfigDetails (2, 2, 48000.0, 512);
        instance->prepareToPlay (48000.0, 512);

        juce::AudioBuffer<float> buffer (2, 512);
        juce::AudioBuffer<float> reference (2, 512);

        for (int channel = 0; channel < 2; ++channel)
            for (int i = 0; i < 512; ++i)
                reference.setSample (channel, i, 0.25f * std::sin (0.05f * (float) i * (float) (channel + 1)));

        buffer.makeCopyOf (reference);
        juce::MidiBuffer midi;
        instance->processBlock (buffer, midi);

        auto maxDifference = 0.0f;

        for (int channel = 0; channel < 2; ++channel)
            for (int i = 0; i < 512; ++i)
                maxDifference = juce::jmax (maxDifference, std::abs (buffer.getSample (channel, i) - reference.getSample (channel, i)));

        check (maxDifference == 0.0f, "non-ARA processBlock passes the input through (max difference " + juce::String (maxDifference) + ")");
        instance->releaseResources();
    }

    void createEditor()
    {
        if (! check (instance->hasEditor(), "plugin has an editor"))
            return finish();

        editor.reset (instance->createEditorAndMakeActive());

        if (! check (editor != nullptr, "editor created"))
            return finish();

        window = std::make_unique<OffscreenWindow> (*editor);

        if (traceDir == juce::File())
        {
            report ("no trace dir given; not waiting for the page");
            return finish();
        }

        waitStart = juce::Time::getMillisecondCounter();
        startTimer (200);
    }

    void timerCallback() override
    {
        bool shown = false;

        for (const auto& file : traceDir.findChildFiles (juce::File::findFiles, false, "gliss-ara-*.log"))
            if (file.loadFileAsString().contains ("editor: not bound to an ARA document"))
                shown = true;

        if (shown)
        {
            stopTimer();
            check (true, "editor without an ARA document shows the notice (" + juce::String ((int) (juce::Time::getMillisecondCounter() - waitStart)) + " ms)");
            return finish();
        }

        if (juce::Time::getMillisecondCounter() - waitStart > 10000)
        {
            stopTimer();
            check (false, "editor did not report the notice within 10 s");
            finish();
        }
    }

    bool check (bool ok, const juce::String& what)
    {
        report ((ok ? "PASS " : "FAIL ") + what);

        if (! ok)
            ++failures;

        return ok;
    }

    void report (const juce::String& line)
    {
        std::printf ("%s\n", line.toRawUTF8());
        reportFile.appendText (line + "\n");
    }

    void finish()
    {
        report (failures == 0 ? "RESULT OK" : "RESULT FAILED (" + juce::String (failures) + ")");
        setApplicationReturnValue (failures == 0 ? 0 : 1);

        // DAW と同じく、エディタを閉じてもメッセージループを回し続け、少し待ってからプラグインを手放す
        // （WebView2 の後始末はメッセージループの上で非同期に進む）。
        report ("teardown: closing the editor");
        window.reset();
        editor.reset();
        report ("teardown: editor closed");

        juce::Timer::callAfterDelay (teardownWaitMs, [this]
        {
            report ("teardown: releasing the ARA factory and the plugin instance");
            araFactory = {};
            instance.reset();
            report ("teardown: plugin instance released");
            juce::Timer::callAfterDelay (teardownWaitMs, [this] { report ("teardown: quit"); quit(); });
        });
    }

    juce::AudioPluginFormatManager formatManager;
    juce::PluginDescription description;
    juce::ARAFactoryWrapper araFactory;
    std::unique_ptr<juce::AudioPluginInstance> instance;
    std::unique_ptr<juce::AudioProcessorEditor> editor;
    std::unique_ptr<OffscreenWindow> window;
    std::unique_ptr<EditorCheck> editorCheck;
    std::unique_ptr<AraEditorCheck> araEditorCheck;
    std::unique_ptr<AraPlaybackCheck> araPlaybackCheck;
    std::unique_ptr<AraPreviewCheck> araPreviewCheck;

    juce::File reportFile, traceDir;
    juce::String pluginPath;
    juce::uint32 waitStart = 0;
    bool skipEditor = false;
    int failures = 0;
    static constexpr int teardownWaitMs = 1500;
};

START_JUCE_APPLICATION (HostCheckApplication)
