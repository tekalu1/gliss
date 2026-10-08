#pragma once

// GlissHostCheck --ara-preview: 同じ ARA ドキュメントに結び付けた 2 つのインスタンスの EditorRenderer のうち、試聴を求めたほうだけが
// 試聴の音を足すことを確かめる（エンジンは使わない。GLISS_ENGINE_DISABLED=1・GLISS_TEST_BRIDGE_DIR を付けて呼ぶ。GLISS_TEST_HOOKS のビルド）。
//
//   GlissHostCheck --ara-preview <結果を書くファイル> <Gliss.vst3> <GLISS_TEST_BRIDGE_DIR と同じフォルダ> <GLISS_ARA_TRACE_DIR と同じフォルダ>
//   GlissHostCheck --ara-preview-playback ...（同じ引数。変種: どの EditorRenderer にもリージョンを渡さず、PlaybackRenderer にだけ渡す）
//
// 作るもの: 1 つのソースに修飾 N・M（それぞれリージョンが 1 つ）。インスタンス 1 の EditorRenderer にだけ N のリージョンを渡し、
// インスタンス 2 には何も渡さない（M のリージョンはどの EditorRenderer にも渡さない＝M を持つ renderer が無い）。
//   1. N の試聴を、インスタンス 1 の EditorRenderer から求める: インスタンス 1 だけに音が乗る（N を持つ renderer が足す）
//   2. 止める
//   3. M の試聴を、インスタンス 2 の EditorRenderer から求める: M を持つ renderer が無いので絞れないが、求めた側（インスタンス 2）だけに
//      音が乗る。前の所有者（インスタンス 1）が持ち越して足し続けない
// 試聴の呼び出しは、プラグインの試験用の口（TestBridge の "@preview"。画面の preview の代わり）で行う。requester（求めた側の
// EditorRenderer の id）は、本物の GlissEditor が画面の preview に足すものと同じ名前。id はプラグインのログ（preview: editor renderer N created）から読む。

#include <juce_audio_processors/juce_audio_processors.h>

#include <array>
#include <atomic>
#include <cmath>
#include <functional>
#include <memory>
#include <vector>

class AraPreviewCheck final : private juce::Timer
{
public:
    using Report = std::function<void (const juce::String&)>;
    using Check = std::function<bool (bool, const juce::String&)>;

    /** playbackAssigned: false = 修飾 N のリージョンを、インスタンス 1 の EditorRenderer に渡す（EditorRenderer の割り当てで絞れるホスト）。
        true = どの EditorRenderer にもリージョンを渡さず、PlaybackRenderer にだけ渡す（N はインスタンス 1、M はインスタンス 2。
        Studio Pro のように EditorRenderer にリージョンを割り当てないホスト）。後者は、試聴を求めた側（requester）を、わざと音を持たない側にする。 */
    AraPreviewCheck (Report reportIn, Check checkIn, juce::File pluginIn, juce::File bridgeDirIn, juce::File traceDirIn,
                     bool playbackAssignedIn = false)
        : report (std::move (reportIn)), check (std::move (checkIn)), plugin (std::move (pluginIn)),
          bridgeDir (std::move (bridgeDirIn)), traceDir (std::move (traceDirIn)), playbackAssigned (playbackAssignedIn)
    {
        // 試聴する修飾ごとの「求める側」と「音が乗るはずの側」（インスタンスの番号）
        if (playbackAssigned) { requesterOfN = 1; expectedForN = 0; requesterOfM = 0; expectedForM = 1; }
        voice.rate = voiceRate;
        voice.samples.resize ((size_t) (4.0 * voiceRate));

        for (size_t i = 0; i < voice.samples.size(); ++i)
            voice.samples[i] = (float) (0.25 * std::sin (juce::MathConstants<double>::twoPi * 440.0 * (double) i / voiceRate));
    }

    ~AraPreviewCheck() override
    {
        stopTimer();
        releaseAll();
    }

    void start (std::function<void()> onFinishedIn)
    {
        onFinished = std::move (onFinishedIn);
        keepLoaded.open (plugin.getFullPathName());   // AraPlaybackCheck と同じ理由（ファクトリが先に手放されても DLL を外さない）
        formatManager.addFormat (std::make_unique<juce::VST3PluginFormat>());

        juce::OwnedArray<juce::PluginDescription> descriptions;
        formatManager.getFormat (0)->findAllTypesForFile (descriptions, plugin.getFullPathName());

        if (! check (descriptions.size() == 1 && descriptions[0]->hasARAExtension, "Gliss.vst3 found with an ARA extension"))
            return finish();

        description = *descriptions[0];
        createInstance (0);
    }

private:
    //==============================================================================
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

    /** 再生していないホスト（試聴だけが鳴る）。 */
    class IdleHead final : public juce::AudioPlayHead
    {
    public:
        juce::Optional<PositionInfo> getPosition() const override
        {
            PositionInfo info;
            info.setIsPlaying (false);
            info.setTimeInSamples (0);
            info.setTimeInSeconds (0.0);
            return info;
        }
    };

    struct Instance
    {
        std::unique_ptr<juce::AudioPluginInstance> plugin;
        juce::ARAHostModel::PlugInExtensionInstance extension;
        juce::ARAHostModel::PlaybackRendererInterface playbackRenderer;
        juce::ARAHostModel::EditorRendererInterface editorRenderer;
        juce::AudioBuffer<float> output { 1, blockSize };
        float peak = 0.0f;                 // 直近の 1 ブロック
        float peakSince = 0.0f;            // markWindow からの最大
    };

    template <typename Ref, typename T>
    static Ref hostRef (T* p) { return reinterpret_cast<Ref> (p); }

    //==============================================================================
    void createInstance (int index)
    {
        formatManager.createPluginInstanceAsync (description, voiceRate, blockSize,
                                                 [this, index] (std::unique_ptr<juce::AudioPluginInstance> created, const juce::String& error)
        {
            if (! check (created != nullptr, "plugin instance " + juce::String (index + 1) + " created " + error))
                return finish();

            instances[(size_t) index].plugin = std::move (created);

            if (index == 0)
            {
                juce::createARAFactoryAsync (*instances[0].plugin, [this] (juce::ARAFactoryWrapper wrapper)
                {
                    if (! check (wrapper.get() != nullptr, "ARA factory created"))
                        return finish();

                    factory = std::move (wrapper);
                    createDocument();
                    createInstance (1);
                });
            }
            else
            {
                bindAndPrepare();
            }
        });
    }

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

            auto as = juce::ARAHostModel::AudioSource::getEmptyProperties();
            as.name = "hostcheck voice";
            as.persistentID = "hostcheck-source";
            as.sampleCount = (ARA::ARASampleCount) voice.samples.size();
            as.sampleRate = voice.rate;
            as.channelCount = 1;
            as.merits64BitSamples = false;
            audioSource = std::make_unique<juce::ARAHostModel::AudioSource> (hostRef<ARA::ARAAudioSourceHostRef> (&voice), dc, as);

            for (int i = 0; i < 2; ++i)
            {
                auto rs = juce::ARAHostModel::RegionSequence::getEmptyProperties();
                rs.name = i == 0 ? "Track N" : "Track M";
                rs.orderIndex = i;
                rs.musicalContextRef = musicalContext->getPluginRef();
                regionSequences.push_back (std::make_unique<juce::ARAHostModel::RegionSequence> (
                    hostRef<ARA::ARARegionSequenceHostRef> (&refs[i * 3]), dc, rs));

                auto am = juce::ARAHostModel::AudioModification::getEmptyProperties();
                am.name = i == 0 ? "Take N" : "Take M";
                am.persistentID = i == 0 ? modificationN : modificationM;
                audioModifications.push_back (std::make_unique<juce::ARAHostModel::AudioModification> (
                    hostRef<ARA::ARAAudioModificationHostRef> (&refs[i * 3 + 1]), dc, *audioSource, am));

                auto pr = juce::ARAHostModel::PlaybackRegion::getEmptyProperties();
                pr.transformationFlags = ARA::kARAPlaybackTransformationNoChanges;
                pr.startInModificationTime = 0.0;
                pr.durationInModificationTime = duration;
                pr.startInPlaybackTime = i * 10.0;
                pr.durationInPlaybackTime = duration;
                pr.musicalContextRef = musicalContext->getPluginRef();
                pr.regionSequenceRef = regionSequences.back()->getPluginRef();
                pr.name = i == 0 ? "Take N" : "Take M";
                playbackRegions.push_back (std::make_unique<juce::ARAHostModel::PlaybackRegion> (
                    hostRef<ARA::ARAPlaybackRegionHostRef> (&refs[i * 3 + 2]), dc, *audioModifications.back(), pr));
            }
        }

        audioSource->enableAudioSourceSamplesAccess (true);
    }

    void bindAndPrepare()
    {
        const auto roles = ARA::kARAPlaybackRendererRole | ARA::kARAEditorRendererRole;

        for (size_t i = 0; i < instances.size(); ++i)
        {
            auto& in = instances[i];
            in.extension = document->bindDocumentToPluginInstance (*in.plugin, roles, roles);

            if (! check (in.extension.isValid(), "plugin instance " + juce::String ((int) i + 1) + " bound to the document"))
                return finish();

            in.playbackRenderer = in.extension.getPlaybackRendererInterface();
            in.editorRenderer = in.extension.getEditorRendererInterface();
        }

        if (playbackAssigned)
        {
            // どの EditorRenderer にもリージョンを渡さない。PlaybackRenderer には再生のために必ず渡す（ARA の規則）: N はインスタンス 1、M はインスタンス 2
            instances[0].playbackRenderer.add (*playbackRegions[0]);
            instances[1].playbackRenderer.add (*playbackRegions[1]);
        }
        else
        {
            // インスタンス 1 の EditorRenderer にだけ N のリージョンを渡す。インスタンス 2 は空、M のリージョンはどこにも渡さない
            instances[0].editorRenderer.add (*playbackRegions[0]);
        }

        for (auto& in : instances)
        {
            in.plugin->setPlayConfigDetails (0, 1, voiceRate, blockSize);
            in.plugin->setPlayHead (&idleHead);
            in.plugin->prepareToPlay (voiceRate, blockSize);
        }

        if (! readRendererIds())
            return finish();

        report (playbackAssigned
                    ? "document: source + modifications N, M; no editor renderer has a region; instance 1's playback renderer has N, instance 2's has M"
                      "; the window owner (requester) is the instance that does not play the modification"
                    : "document: source + modifications N, M; instance 1's editor renderer " + juce::String ((juce::int64) rendererIds[0])
                          + " covers N, instance 2's editor renderer " + juce::String ((juce::int64) rendererIds[1]) + " covers nothing");
        stage = Stage::startN;
        stageStart = juce::Time::getMillisecondCounter();
        startTimer (30);
    }

    bool readRendererIds()
    {
        std::vector<juce::int64> ids;

        for (const auto& file : traceDir.findChildFiles (juce::File::findFiles, false, "gliss-ara-*.log"))
            for (const auto& line : juce::StringArray::fromLines (file.loadFileAsString()))
                if (line.contains ("preview: editor renderer ") && line.endsWith (" created"))
                    ids.push_back (line.fromFirstOccurrenceOf ("preview: editor renderer ", false, false).getLargeIntValue());

        if (! check (ids.size() == 2, "two editor renderers were created (" + juce::String ((int) ids.size()) + " found in the trace log)"))
            return false;

        rendererIds[0] = (juce::uint64) ids[0];
        rendererIds[1] = (juce::uint64) ids[1];
        return true;
    }

    //==============================================================================
    void markWindow()
    {
        for (auto& in : instances)
            in.peakSince = 0.0f;

        windowStart = juce::Time::getMillisecondCounter();
    }

    void renderBoth()
    {
        for (auto& in : instances)
        {
            in.output.clear();
            midi.clear();
            in.plugin->processBlock (in.output, midi);
            in.peak = in.output.getMagnitude (0, in.output.getNumSamples());
            in.peakSince = juce::jmax (in.peakSince, in.peak);
        }
    }

    juce::int64 requestCounter = 0;

    /** 試験用の口（"@preview"）へ試聴の呼び出しを出す。答え（{ok, reason}）は pollAnswer で読む。 */
    void sendPreview (const juce::String& op, juce::var args)
    {
        auto* request = new juce::DynamicObject();
        request->setProperty ("tool", "@preview");
        request->setProperty ("op", op);
        request->setProperty ("args", std::move (args));
        answerFile = bridgeDir.getChildFile ("pv" + juce::String (++requestCounter).paddedLeft ('0', 3) + ".result.json");
        const auto call = bridgeDir.getChildFile ("pv" + juce::String (requestCounter).paddedLeft ('0', 3) + ".call.json");
        call.replaceWithText (juce::JSON::toString (juce::var (request), true));
    }

    bool pollAnswer (juce::var& answer)
    {
        if (! answerFile.existsAsFile())
            return false;

        answer = juce::JSON::parse (answerFile.loadFileAsString());
        return answer.isObject();
    }

    juce::var startArgs (const juce::String& modification, juce::uint64 requester)
    {
        auto* o = new juce::DynamicObject();
        o->setProperty ("local", true);
        o->setProperty ("ara_id", modification);
        o->setProperty ("note", "n1");
        o->setProperty ("cents", 0.0);
        o->setProperty ("start_sec", 0.5);
        o->setProperty ("end_sec", 2.0);
        o->setProperty ("allow_stale", true);
        o->setProperty ("loop", true);
        o->setProperty ("requester", (juce::int64) requester);
        return juce::var (o);
    }

    void timerCallback() override
    {
        renderBoth();
        const auto now = juce::Time::getMillisecondCounter();
        const auto inStage = (int) (now - stageStart);
        juce::var answer;

        switch (stage)
        {
            case Stage::startN:
                sendPreview ("start", startArgs (modificationN, rendererIds[(size_t) requesterOfN]));
                stage = Stage::waitN;
                stageStart = now;
                break;

            case Stage::waitN:
                if (! pollAnswer (answer) && inStage < timeoutMs)
                    break;

                if (! check ((bool) answer.getProperty ("ok", false), "preview of N started (" + (answer.isObject() ? answer.getProperty ("reason", "").toString() : juce::String ("no answer")) + ")"))
                    return finish();

                stage = Stage::listenN;
                stageStart = now;
                markWindow();
                break;

            case Stage::listenN:
                if (inStage < listenMs && ! (instances[(size_t) expectedForN].peakSince > audible && inStage > 400))
                    break;

                report ("N: instance 1 peak " + juce::String (instances[0].peakSince, 3) + ", instance 2 peak " + juce::String (instances[1].peakSince, 3));
                check (instances[(size_t) expectedForN].peakSince > audible,
                       playbackAssigned ? "N is heard on the instance whose playback renderer has it (the requester is the other instance)"
                                        : "N is heard on the instance whose editor renderer covers it");
                check (instances[(size_t) (1 - expectedForN)].peakSince < silent, "N is not added by the other instance");
                sendPreview ("stop", juce::var());
                stage = Stage::stopN;
                stageStart = now;
                break;

            case Stage::stopN:
                if (inStage < settleMs)
                    break;

                check (instances[0].peak < silent && instances[1].peak < silent, "both instances are silent after the preview stops");
                stage = Stage::startM;
                stageStart = now;
                break;

            case Stage::startM:
                // M を持つ EditorRenderer は無い。求めた側はインスタンス 2
                sendPreview ("start", startArgs (modificationM, rendererIds[(size_t) requesterOfM]));
                stage = Stage::waitM;
                stageStart = now;
                break;

            case Stage::waitM:
                if (! pollAnswer (answer) && inStage < timeoutMs)
                    break;

                if (! check ((bool) answer.getProperty ("ok", false), "preview of M started (" + (answer.isObject() ? answer.getProperty ("reason", "").toString() : juce::String ("no answer")) + ")"))
                    return finish();

                stage = Stage::listenM;
                stageStart = now;
                markWindow();
                break;

            case Stage::listenM:
                if (inStage < listenMs)
                    break;

                report ("M: instance 1 peak " + juce::String (instances[0].peakSince, 3) + ", instance 2 peak " + juce::String (instances[1].peakSince, 3));
                check (instances[(size_t) expectedForM].peakSince > audible,
                       playbackAssigned ? "M is heard on the instance whose playback renderer has it (the requester is the other instance)"
                                        : "M (no editor renderer covers it) is heard on the instance that asked");
                check (instances[(size_t) (1 - expectedForM)].peakSince < silent,
                       playbackAssigned ? "the instance that asked (the window owner) does not add M" : "the previous owner (instance 1) does not keep adding the preview");
                sendPreview ("stop", juce::var());
                stage = Stage::stopM;
                stageStart = now;
                break;

            case Stage::stopM:
                if (inStage < settleMs)
                    break;

                stopTimer();
                finish();
                break;
        }
    }

    void finish()
    {
        stopTimer();
        releaseAll();
        report ("teardown: document released");

        if (onFinished != nullptr)
            std::exchange (onFinished, nullptr)();
    }

    void releaseAll()
    {
        for (auto& in : instances)
            if (in.plugin != nullptr)
                in.plugin->releaseResources();

        for (auto& in : instances)
        {
            in.editorRenderer = {};
            in.playbackRenderer = {};
            in.extension = {};

            if (in.plugin != nullptr)
                in.plugin->setPlayHead (nullptr);

            in.plugin.reset();
        }

        playbackRegions.clear();
        audioModifications.clear();
        regionSequences.clear();
        audioSource.reset();
        musicalContext.reset();
        document.reset();
        factory = {};
    }

    enum class Stage { startN, waitN, listenN, stopN, startM, waitM, listenM, stopM };

    static constexpr double voiceRate = 44100.0;
    static constexpr int blockSize = 512;
    static constexpr float audible = 0.02f;
    static constexpr float silent = 1.0e-4f;
    static constexpr int timeoutMs = 6000, listenMs = 1200, settleMs = 600;
    static constexpr const char* modificationN = "hostcheck-modification-n";
    static constexpr const char* modificationM = "hostcheck-modification-m";

    juce::DynamicLibrary keepLoaded;
    Report report;
    Check check;
    juce::File plugin, bridgeDir, traceDir, answerFile;
    std::function<void()> onFinished;
    Voice voice;
    IdleHead idleHead;
    juce::MidiBuffer midi;
    Stage stage = Stage::startN;
    juce::uint32 stageStart = 0, windowStart = 0;
    juce::uint64 rendererIds[2] {};
    bool playbackAssigned = false;
    int requesterOfN = 0, expectedForN = 0, requesterOfM = 1, expectedForM = 1;

    juce::PluginDescription description;
    juce::AudioPluginFormatManager formatManager;
    std::array<Instance, 2> instances;
    juce::ARAFactoryWrapper factory;
    std::unique_ptr<juce::ARAHostDocumentController> document;
    std::unique_ptr<juce::ARAHostModel::MusicalContext> musicalContext;
    int refs[6] {};
    std::unique_ptr<juce::ARAHostModel::AudioSource> audioSource;
    std::vector<std::unique_ptr<juce::ARAHostModel::RegionSequence>> regionSequences;
    std::vector<std::unique_ptr<juce::ARAHostModel::AudioModification>> audioModifications;
    std::vector<std::unique_ptr<juce::ARAHostModel::PlaybackRegion>> playbackRegions;
};
