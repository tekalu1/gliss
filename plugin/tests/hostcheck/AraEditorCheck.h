#pragma once

// GlissHostCheck --ara-editor: JUCE の ARA ホスト（juce_ARAHosting）で Gliss.vst3 に本物のドキュメント（合成の歌声もどきの
// AudioSource・AudioModification・PlaybackRegion）を作り、インスタンスを全部の役（再生・試聴・エディタ）で結び付けてエディタを開く。
// DAW でエディタを開いたときと同じ経路（DocumentController → GlissEditor → DocumentBridge → エンジン）を通す。
//
//   GlissHostCheck --ara-editor <結果を書くファイル> <Gliss.vst3> <GLISS_ARA_TRACE_DIR と同じフォルダ> [--timeout <秒>]
//
// プラグインの中の画面には外から触れないので、プラグインが GLISS_ARA_TRACE_DIR に書くログで確かめる:
//   1. エディタがドキュメントにつながる（editor: opened with a document）・画面が読み込まれる（editor: ui-ready）・bootstrap が呼ばれる
//   2. エンジンが起動して作業場所を開き（sync: engine ready）、ソースを読んで修飾を登録する（sync: ara_set_modification）
//   3. 画面の engineCall がエンジンに届いて成功する（list_tracks・select_track・export_view_data。export_view_data は解析の後に呼ばれ、
//      画面はその JSON を /fs/ で読む）
// エンジンの起動の設定（GLISS_ENGINE_PYTHON・GLISS_ENGINE_CWD・VOCAL_ENGINE_WORK_DIR など）は呼ぶ側（test-plugin.ps1）が環境変数で渡す。
// 窓は画面の外に置き、SW_SHOWNA（前面にも入力の対象にもならない）で出す。

#include <juce_audio_processors/juce_audio_processors.h>
#include <juce_gui_extra/juce_gui_extra.h>

#include <atomic>
#include <cmath>
#include <functional>
#include <memory>
#include <vector>

class AraEditorCheck final : private juce::Timer
{
public:
    using Report = std::function<void (const juce::String&)>;
    using Check = std::function<bool (bool, const juce::String&)>;

    AraEditorCheck (Report reportIn, Check checkIn, juce::File pluginIn, juce::File traceDirIn, int timeoutSecIn,
                    bool measureAuditionIn = false, int modificationCountIn = 1, int voiceSecondsIn = 6,
                    int auditionCountIn = 1)
        : report (std::move (reportIn)), check (std::move (checkIn)), plugin (std::move (pluginIn)),
          traceDir (std::move (traceDirIn)), timeoutMs (timeoutSecIn * 1000), measureAudition (measureAuditionIn),
          modificationCount (juce::jlimit (1, 34, modificationCountIn)), voiceSeconds (voiceSecondsIn >= 180 ? 180 : 6),
          auditionCount (juce::jlimit (1, 3, auditionCountIn))
    {
        makeVoice();
    }

    ~AraEditorCheck() override
    {
        stopTimer();
        releaseAll();
    }

    void start (std::function<void()> onFinishedIn)
    {
        onFinished = std::move (onFinishedIn);
        formatManager.addFormat (std::make_unique<juce::VST3PluginFormat>());

        juce::OwnedArray<juce::PluginDescription> descriptions;
        formatManager.getFormat (0)->findAllTypesForFile (descriptions, plugin.getFullPathName());

        if (! check (descriptions.size() == 1 && descriptions[0]->hasARAExtension, "Gliss.vst3 found with an ARA extension"))
            return finish();

        formatManager.createPluginInstanceAsync (*descriptions[0], voiceRate, 512,
                                                 [this] (std::unique_ptr<juce::AudioPluginInstance> created, const juce::String& error)
        {
            if (! check (created != nullptr, "plugin instance created " + error))
                return finish();

            instance = std::move (created);
            juce::createARAFactoryAsync (*instance, [this] (juce::ARAFactoryWrapper wrapper)
            {
                if (! check (wrapper.get() != nullptr, "ARA factory created"))
                    return finish();

                factory = std::move (wrapper);
                createDocument();
            });
        });
    }

private:
    //==============================================================================
    // ホストの口（ARA::Host の最小の実装）
    struct Voice
    {
        std::vector<float> samples;
        double rate = 44100.0;
    };

    struct Reader
    {
        const Voice* voice = nullptr;
        bool use64BitSamples = false;
    };

    class AudioAccess final : public ARA::Host::AudioAccessControllerInterface
    {
    public:
        ARA::ARAAudioReaderHostRef createAudioReaderForSource (ARA::ARAAudioSourceHostRef source, bool use64BitSamples) noexcept override
        {
            return reinterpret_cast<ARA::ARAAudioReaderHostRef> (new Reader { reinterpret_cast<const Voice*> (source), use64BitSamples });
        }

        bool readAudioSamples (ARA::ARAAudioReaderHostRef readerRef, ARA::ARASamplePosition position,
                               ARA::ARASampleCount count, void* const buffers[]) noexcept override
        {
            const auto* reader = reinterpret_cast<const Reader*> (readerRef);
            const auto& samples = reader->voice->samples;

            for (ARA::ARASampleCount i = 0; i < count; ++i)
            {
                const auto p = position + i;
                const auto v = (p >= 0 && p < (ARA::ARASamplePosition) samples.size()) ? samples[(size_t) p] : 0.0f;

                if (reader->use64BitSamples)
                    static_cast<double*> (buffers[0])[i] = v;
                else
                    static_cast<float*> (buffers[0])[i] = v;
            }

            return true;
        }

        void destroyAudioReader (ARA::ARAAudioReaderHostRef readerRef) noexcept override
        {
            delete reinterpret_cast<Reader*> (readerRef);
        }
    };

    class Archiving final : public ARA::Host::ArchivingControllerInterface
    {
    public:
        ARA::ARASize getArchiveSize (ARA::ARAArchiveReaderHostRef) noexcept override { return 0; }
        bool readBytesFromArchive (ARA::ARAArchiveReaderHostRef, ARA::ARASize, ARA::ARASize, ARA::ARAByte[]) noexcept override { return false; }
        bool writeBytesToArchive (ARA::ARAArchiveWriterHostRef, ARA::ARASize, ARA::ARASize, const ARA::ARAByte[]) noexcept override { return false; }
        void notifyDocumentArchivingProgress (float) noexcept override {}
        void notifyDocumentUnarchivingProgress (float) noexcept override {}
        ARA::ARAPersistentID getDocumentArchiveID (ARA::ARAArchiveReaderHostRef) noexcept override { return nullptr; }
    };

    class Playback final : public ARA::Host::PlaybackControllerInterface
    {
    public:
        void requestStartPlayback() noexcept override {}
        void requestStopPlayback() noexcept override {}
        void requestSetPlaybackPosition (ARA::ARATimePosition) noexcept override {}
        void requestSetCycleRange (ARA::ARATimePosition, ARA::ARATimeDuration) noexcept override {}
        void requestEnableCycle (bool) noexcept override {}
    };

    //==============================================================================
    void makeVoice()
    {
        // engine/tests/test_ara_tools.py の _voice と同じ作り（倍音 8 本・5.5 Hz ±30 セントのビブラート・5 ノート、44.1 kHz・モノラル）
        struct Note { double start, length; int midi; };
        std::vector<Note> notes;
        if (voiceSeconds == 180)
        {
            notes.reserve (300);
            for (int i = 0; i < 300; ++i)
                notes.push_back ({ 0.15 + i * 0.6, 0.32, 60 + (i % 5) });
        }
        else
            notes = { { 0.5, 0.55, 60 }, { 1.65, 0.55, 64 }, { 2.8, 0.55, 67 }, { 3.95, 0.55, 65 }, { 5.1, 0.55, 62 } };
        voice.rate = voiceRate;
        voice.samples.assign ((size_t) ((voiceSeconds == 180 ? 180.0 : 6.2) * voiceRate), 0.0f);

        for (const auto& n : notes)
        {
            const auto a = (size_t) (n.start * voiceRate), b = (size_t) ((n.start + n.length) * voiceRate);
            const auto f0 = 440.0 * std::pow (2.0, (n.midi - 69) / 12.0);
            double phase = 0.0;

            for (size_t i = a; i < b && i < voice.samples.size(); ++i)
            {
                const auto t = (double) (i - a) / voiceRate;
                const auto vib = 30.0 * std::sin (juce::MathConstants<double>::twoPi * 5.5 * t) * juce::jlimit (0.0, 1.0, (t - 0.12) / 0.1);
                phase += juce::MathConstants<double>::twoPi * f0 * std::pow (2.0, vib / 1200.0) / voiceRate;
                double s = 0.0;
                for (int k = 0; k < 8; ++k)
                    s += std::pow (0.6, k) * std::sin ((k + 1) * phase);
                voice.samples[i] += (float) (0.25 * s * juce::jmin (1.0, t / 0.03, (n.length - t) / 0.04));
            }
        }
    }

    template <typename Ref, typename T>
    static Ref hostRef (T* p) { return reinterpret_cast<Ref> (p); }

    void createDocument()
    {
        document = juce::ARAHostDocumentController::create (std::move (factory), "GlissHostCheck",
                                                            std::make_unique<AudioAccess>(), std::make_unique<Archiving>(),
                                                            nullptr, nullptr, std::make_unique<Playback>());

        if (! check (document != nullptr, "ARA document controller created"))
            return finish();

        auto& dc = document->getDocumentController();
        const auto duration = (double) voice.samples.size() / voice.rate;

        {
            const juce::ARAEditGuard guard (dc);

            auto mc = juce::ARAHostModel::MusicalContext::getEmptyProperties();
            mc.name = "Song";
            mc.orderIndex = 0;
            musicalContext = std::make_unique<juce::ARAHostModel::MusicalContext> (hostRef<ARA::ARAMusicalContextHostRef> (&voice), dc, mc);

            const auto sequenceCount = modificationCount > 1 ? 2 : 1;
            for (int i = 0; i < sequenceCount; ++i)
            {
                auto rs = juce::ARAHostModel::RegionSequence::getEmptyProperties();
                const auto sequenceName = "Vocal " + juce::String (i + 1);
                rs.name = sequenceName.toRawUTF8();
                rs.orderIndex = i;
                rs.musicalContextRef = musicalContext->getPluginRef();
                regionSequences.push_back (std::make_unique<juce::ARAHostModel::RegionSequence> (
                    hostRef<ARA::ARARegionSequenceHostRef> (&sequenceRefs[(size_t) i]), dc, rs));
            }

            auto as = juce::ARAHostModel::AudioSource::getEmptyProperties();
            as.name = "hostcheck voice";
            as.persistentID = "hostcheck-source";
            as.sampleCount = (ARA::ARASampleCount) voice.samples.size();
            as.sampleRate = voice.rate;
            as.channelCount = 1;
            as.merits64BitSamples = false;
            audioSource = std::make_unique<juce::ARAHostModel::AudioSource> (hostRef<ARA::ARAAudioSourceHostRef> (&voice), dc, as);

            modificationRefs.resize ((size_t) modificationCount);
            regionRefs.resize ((size_t) modificationCount);
            for (int i = 0; i < modificationCount; ++i)
            {
                auto am = juce::ARAHostModel::AudioModification::getEmptyProperties();
                const auto modificationName = "Vocal take " + juce::String (i + 1);
                am.name = modificationName.toRawUTF8();
                const auto persistentID = i == 0 ? juce::String ("hostcheck-modification")
                                                  : juce::String ("hostcheck-modification-") + juce::String (i + 1);
                am.persistentID = persistentID.toRawUTF8();
                audioModifications.push_back (std::make_unique<juce::ARAHostModel::AudioModification> (
                    hostRef<ARA::ARAAudioModificationHostRef> (&modificationRefs[(size_t) i]), dc, *audioSource, am));

                auto pr = juce::ARAHostModel::PlaybackRegion::getEmptyProperties();
                pr.transformationFlags = ARA::kARAPlaybackTransformationNoChanges;
                pr.startInModificationTime = 0.0;
                pr.durationInModificationTime = duration;
                pr.startInPlaybackTime = 2.0 + i * 5.1;
                pr.durationInPlaybackTime = duration;
                pr.musicalContextRef = musicalContext->getPluginRef();
                pr.regionSequenceRef = regionSequences[(size_t) (i % sequenceCount)]->getPluginRef();
                const auto regionName = "Vocal take " + juce::String (i + 1);
                pr.name = regionName.toRawUTF8();
                playbackRegions.push_back (std::make_unique<juce::ARAHostModel::PlaybackRegion> (
                    hostRef<ARA::ARAPlaybackRegionHostRef> (&regionRefs[(size_t) i]), dc, *audioModifications.back(), pr));
            }
        }

        audioSource->enableAudioSourceSamplesAccess (true);
        report ("document: 1 source (" + juce::String (duration, 2) + " s, " + juce::String (voice.rate)
                + " Hz), " + juce::String (modificationCount) + " modifications/regions across "
                + juce::String (2.0 + (modificationCount - 1) * 5.1 + duration, 2) + " song seconds");

        const auto roles = ARA::kARAPlaybackRendererRole | ARA::kARAEditorRendererRole | ARA::kARAEditorViewRole;
        extension = document->bindDocumentToPluginInstance (*instance, roles, roles);

        if (! check (extension.isValid(), "plugin instance bound to the document (playback renderer, editor renderer, editor view)"))
            return finish();

        // DAW と同じく、描画の役は再生のリージョンを持たせて準備する（エディタは描画しなくても開ける）。
        playbackRenderer = extension.getPlaybackRendererInterface();   // 持っている間だけ登録が続く（RAII）
        for (const auto& region : playbackRegions)
            playbackRenderer.add (*region);
        instance->setPlayConfigDetails (0, 1, voice.rate, 512);
        instance->prepareToPlay (voice.rate, 512);

        editor.reset (instance->createEditorAndMakeActive());

        if (! check (editor != nullptr, "editor created"))
            return finish();

        window = std::make_unique<OffscreenWindow> (*editor);
        waitStart = juce::Time::getMillisecondCounter();
        startTimer (250);
    }

    //==============================================================================
    void timerCallback() override
    {
        if (auditionStarted) return tickAudition();
        juce::String log;

        for (const auto& file : traceDir.findChildFiles (juce::File::findFiles, false, "gliss-ara-*.log"))
            log << file.loadFileAsString();

        struct Expectation { const char* text; const char* what; };
        static const Expectation expectations[] {
            { "editor: opened with a document", "the editor found the document (DocumentBridge)" },
            { "editor: ui-ready", "the page loaded (ui-ready)" },
            { "bridge: bootstrap", "the page called bootstrap" },
            { "sync: engine ready", "the engine started and opened the work place" },
            { "sync: ara_set_modification hostcheck-modification", "the source was read and the modification registered" },
            { "bridge: engineCall list_tracks ok", "engineCall list_tracks succeeded" },
            { "bridge: engineCall select_track ok", "engineCall select_track succeeded" },
            { "bridge: engineCall export_view_data ok", "engineCall export_view_data succeeded (after the analysis)" },
        };

        bool all = true;

        for (const auto& e : expectations)
            all = all && log.contains (e.text);

        const auto elapsed = (int) (juce::Time::getMillisecondCounter() - waitStart);

        if (! all && elapsed < timeoutMs)
            return;

        stopTimer();

        for (const auto& e : expectations)
            check (log.contains (e.text), juce::String (e.what) + " [" + e.text + "]");

        check (! log.contains ("bridge: refused"), "no /fs/ path was refused");

        juce::StringArray failedCalls;
        for (const auto& line : juce::StringArray::fromLines (log))
            if (line.contains ("bridge: engineCall") && line.contains (" failed: "))
                failedCalls.add (line.fromFirstOccurrenceOf ("bridge: ", false, false));

        report ("engine calls that failed (reported, not checked): " + (failedCalls.isEmpty() ? juce::String ("none") : failedCalls.joinIntoString (" | ")));
        report ("waited " + juce::String (elapsed) + " ms");
        if (measureAudition && all) startAudition();
        else finish();
    }

    void startAudition()
    {
        auditionStarted = true;
        report ("audition ready for external pointer input");
        startTimer (10);
    }

    juce::File auditionSignal (const juce::String& name) const
    {
        return traceDir.getChildFile (name + (auditionIndex == 0 ? juce::String()
            : "-" + juce::String (auditionIndex + 1)) + ".signal");
    }

    void tickAudition()
    {
        const auto now = juce::Time::getMillisecondCounter();
        if (auditionStart == 0 && auditionSignal ("audition-down").existsAsFile())
        {
            auditionStart = now;
            report ("audition down signal ms=" + juce::String (now));
        }
        auditionOutput.clear();
        auditionMidi.clear();
        instance->processBlock (auditionOutput, auditionMidi);
        float peak = auditionOutput.getMagnitude (0, auditionOutput.getNumSamples());
        if (auditionStart != 0 && firstNonzero == 0 && peak > 1.0e-6f)
        {
            firstNonzero = now;
            const auto heardWall = juce::Time::currentTimeMillis();
            const auto sentWall = auditionSignal ("audition-down").getLastModificationTime().toMilliseconds();
            const auto ack = auditionSignal ("audition-down-ack");
            const auto upperMs = heardWall - sentWall;
            report ("audition first nonzero: " + juce::String ((int) (now - auditionStart))
                    + " ms peak=" + juce::String (peak, 7)
                    + " signal-created upper-bound-ms=" + juce::String (upperMs)
                    + (ack.existsAsFile() ? " press-ack lower-bound-ms="
                        + juce::String (heardWall - ack.getLastModificationTime().toMilliseconds()) : juce::String()));
            check (upperMs >= 0 && upperMs <= 500, "synthetic audition first output within 500 ms of pre-press marker");
        }
        if (auditionStop == 0 && auditionSignal ("audition-up").existsAsFile())
        {
            auditionStop = now;
            report ("audition up signal ms=" + juce::String (now));
        }
        if (firstNonzero != 0 && auditionStop != 0 && peak <= 1.0e-6f && now - auditionStop > 20)
        {
            check (true, "audition output stops after pointerup");
            report ("audition stop zero: " + juce::String ((int) (now - auditionStop)) + " ms");
            if (++auditionIndex < auditionCount)
            {
                auditionStart = firstNonzero = auditionStop = 0;
                report ("audition ready for next pointer input");
            }
            else
            {
                stopTimer();
                finish();
            }
        }
        else if (now - waitStart > (juce::uint32) timeoutMs)
        {
            check (false, "audition output became nonzero and then stopped");
            stopTimer();
            finish();
        }
    }

    void finish()
    {
        // DAW と同じく、エディタを閉じてもメッセージループを回し、少し待ってからインスタンス → モデル → ドキュメントの順に手放す
        const auto editorWasOpen = editor != nullptr;
        window.reset();
        editor.reset();

        juce::Timer::callAfterDelay (1500, [this, editorWasOpen]
        {
            if (editorWasOpen)
            {
                juce::String trace;
                for (const auto& file : traceDir.findChildFiles (juce::File::findFiles, false, "gliss-ara-*.log"))
                    trace << file.loadFileAsString();
                check (document != nullptr && trace.contains ("editor: preview stopped on close"),
                       "native editor close stops preview while the ARA document remains");
            }
            releaseAll();
            report ("teardown: document released");

            if (onFinished != nullptr)
                std::exchange (onFinished, nullptr)();
        });
    }

    void releaseAll()
    {
        window.reset();
        editor.reset();

        if (instance != nullptr)
            instance->releaseResources();

        playbackRenderer = {};   // インスタンスを準備していない間に外す
        extension = {};
        instance.reset();
        playbackRegions.clear();
        audioModifications.clear();
        audioSource.reset();
        regionSequences.clear();
        musicalContext.reset();
        document.reset();
        factory = {};
    }

    /** エディタを画面の外に置く窓（Main.cpp の OffscreenWindow と同じ置き方）。 */
    class OffscreenWindow final : public juce::Component
    {
    public:
        explicit OffscreenWindow (juce::AudioProcessorEditor& e) : editorRef (e)
        {
            addAndMakeVisible (editorRef);
            setBounds (-20000, -20000, juce::jmax (800, editorRef.getWidth()), juce::jmax (500, editorRef.getHeight()));
            setWantsKeyboardFocus (false);
            addToDesktop (juce::ComponentPeer::windowIsTemporary);
            setVisible (true);
        }

        void resized() override { editorRef.setBounds (getLocalBounds()); }

    private:
        juce::AudioProcessorEditor& editorRef;
    };

    static constexpr double voiceRate = 44100.0;

    Report report;
    Check check;
    juce::File plugin, traceDir;
    int timeoutMs = 120000;
    bool measureAudition = false, auditionStarted = false;
    int modificationCount = 1;
    int voiceSeconds = 6;
    int auditionCount = 1, auditionIndex = 0;
    juce::uint32 auditionStart = 0, firstNonzero = 0, auditionStop = 0;
    juce::AudioBuffer<float> auditionOutput { 1, 512 };
    juce::MidiBuffer auditionMidi;
    std::function<void()> onFinished;
    Voice voice;

    juce::AudioPluginFormatManager formatManager;
    std::unique_ptr<juce::AudioPluginInstance> instance;
    juce::ARAFactoryWrapper factory;
    std::unique_ptr<juce::ARAHostDocumentController> document;
    std::unique_ptr<juce::ARAHostModel::MusicalContext> musicalContext;
    int sequenceRefs[2] {};
    std::vector<int> modificationRefs, regionRefs;
    std::vector<std::unique_ptr<juce::ARAHostModel::RegionSequence>> regionSequences;
    std::unique_ptr<juce::ARAHostModel::AudioSource> audioSource;
    std::vector<std::unique_ptr<juce::ARAHostModel::AudioModification>> audioModifications;
    std::vector<std::unique_ptr<juce::ARAHostModel::PlaybackRegion>> playbackRegions;
    juce::ARAHostModel::PlugInExtensionInstance extension;
    juce::ARAHostModel::PlaybackRendererInterface playbackRenderer;
    std::unique_ptr<juce::AudioProcessorEditor> editor;
    std::unique_ptr<OffscreenWindow> window;
    juce::uint32 waitStart = 0;
};
