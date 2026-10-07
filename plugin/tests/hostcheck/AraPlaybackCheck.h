#pragma once

// GlissHostCheck --ara-playback: JUCE の ARA ホスト（juce_ARAHosting）で Gliss.vst3 に本物のドキュメントを作り、再生の役で準備した後に
// 起きる変化のあとも、リージョンの原音が鳴り続けることを確かめる（エンジンは使わない。GLISS_ENGINE_DISABLED=1 で呼ぶ）。
//
//   GlissHostCheck --ara-playback <結果を書くファイル> <Gliss.vst3> <GLISS_PLUGIN_LOG_FILE と同じファイル> [--timeout <秒>]
//
// 順に確かめる（どれも prepareToPlay をやり直さない）:
//   1. 準備のとき: 原音が鳴る
//   2. 準備の後に足したリージョン（Studio Pro でトラックを足したとき）が鳴る（準備のときの表に無いリージョン）
//   3. サンプルへのアクセスを切って戻す（先読みのリーダーが、切っている間に読めなかった区間を無音のまま持つ）: 切っている間に読んだ位置が、戻した後に鳴る
//   4. ホストがソースのサンプルの変化を知らせる（doUpdateAudioSourceContent。ARAAudioSourceReader が無効になる）: まだ読んでいない位置が鳴る
//   5. 前から鳴っていた位置が、まだ鳴る
//   6. プラグインのログ（plugin.log）に、準備・リーダーの差し替え・リージョンの同期・解放の行が出る
// 位置を変えて読むのは、同じ位置は先読みのキャッシュから鳴ってしまい、無効になったリーダーに気付けないため。

#include <juce_audio_processors/juce_audio_processors.h>

#include <atomic>
#include <cmath>
#include <functional>
#include <memory>
#include <vector>

class AraPlaybackCheck final : private juce::Timer
{
public:
    using Report = std::function<void (const juce::String&)>;
    using Check = std::function<bool (bool, const juce::String&)>;

    AraPlaybackCheck (Report reportIn, Check checkIn, juce::File pluginIn, juce::File logFileIn, int timeoutSecIn)
        : report (std::move (reportIn)), check (std::move (checkIn)), plugin (std::move (pluginIn)),
          logFile (std::move (logFileIn)), stepTimeoutMs (juce::jlimit (1, 60, timeoutSecIn) * 1000)
    {
        // 440 Hz の正弦波（振幅 0.25）を 70 秒。音の有無だけを見る
        voice.rate = voiceRate;
        voice.samples.resize ((size_t) (70.0 * voiceRate));

        for (size_t i = 0; i < voice.samples.size(); ++i)
            voice.samples[i] = (float) (0.25 * std::sin (juce::MathConstants<double>::twoPi * 440.0 * (double) i / voiceRate));
    }

    ~AraPlaybackCheck() override
    {
        stopTimer();
        releaseAll();
    }

    void start (std::function<void()> onFinishedIn)
    {
        onFinished = std::move (onFinishedIn);

        // ホストが持つ ARA ファクトリが先に手放されると、プラグインの DLL が外れた後にドキュメントを壊してしまう（ARA のホストの JUCE の後始末の順）。
        // 検証が終わるまで DLL を掴んでおく（本物の DAW は ARA のファクトリを文書より長く持つ）。
        keepLoaded.open (plugin.getFullPathName());
        formatManager.addFormat (std::make_unique<juce::VST3PluginFormat>());

        juce::OwnedArray<juce::PluginDescription> descriptions;
        formatManager.getFormat (0)->findAllTypesForFile (descriptions, plugin.getFullPathName());

        if (! check (descriptions.size() == 1 && descriptions[0]->hasARAExtension, "Gliss.vst3 found with an ARA extension"))
            return finish();

        formatManager.createPluginInstanceAsync (*descriptions[0], voiceRate, blockSize,
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

    /** 再生中で、指定の位置にいるホストの再生位置。 */
    class PlayHead final : public juce::AudioPlayHead
    {
    public:
        juce::Optional<PositionInfo> getPosition() const override
        {
            PositionInfo info;
            info.setIsPlaying (true);
            info.setTimeInSamples (samples.load());
            info.setTimeInSeconds ((double) samples.load() / voiceRate);
            return info;
        }

        std::atomic<juce::int64> samples { 0 };
    };

    template <typename Ref, typename T>
    static Ref hostRef (T* p) { return reinterpret_cast<Ref> (p); }

    //==============================================================================
    /** 1 つの手順: action を 1 回行い、probeSec の位置を読み続けて、音が出る（expectAudio）まで待つ。expectAudio が false なら、settleMs だけ読んで次へ。 */
    struct Step
    {
        juce::String name;
        std::function<void()> action;
        double probeSec = 1.0;
        bool expectAudio = true;
        int settleMs = 0;
    };

    void addRegionAfterPrepare()
    {
        // Studio Pro でトラックを足したとき: 準備の後に、同じソースの修飾とリージョンが増える
        auto& dc = document->getDocumentController();
        const juce::ARAEditGuard guard (dc);

        auto am = juce::ARAHostModel::AudioModification::getEmptyProperties();
        am.name = "Added take";
        am.persistentID = "hostcheck-modification-added";
        audioModifications.push_back (std::make_unique<juce::ARAHostModel::AudioModification> (
            hostRef<ARA::ARAAudioModificationHostRef> (&addedModificationRef), dc, *audioSource, am));

        auto pr = juce::ARAHostModel::PlaybackRegion::getEmptyProperties();
        pr.transformationFlags = ARA::kARAPlaybackTransformationNoChanges;
        pr.startInModificationTime = 0.0;
        pr.durationInModificationTime = 10.0;
        pr.startInPlaybackTime = addedRegionStartSec;
        pr.durationInPlaybackTime = 10.0;
        pr.musicalContextRef = musicalContext->getPluginRef();
        pr.regionSequenceRef = regionSequence->getPluginRef();
        pr.name = "Added take";
        playbackRegions.push_back (std::make_unique<juce::ARAHostModel::PlaybackRegion> (
            hostRef<ARA::ARAPlaybackRegionHostRef> (&addedRegionRef), dc, *audioModifications.back(), pr));
        playbackRenderer.add (*playbackRegions.back());      // ホストは、準備済みのレンダラーにも足す
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

            auto rs = juce::ARAHostModel::RegionSequence::getEmptyProperties();
            rs.name = "Vocal";
            rs.orderIndex = 0;
            rs.musicalContextRef = musicalContext->getPluginRef();
            regionSequence = std::make_unique<juce::ARAHostModel::RegionSequence> (
                hostRef<ARA::ARARegionSequenceHostRef> (&sequenceRef), dc, rs);

            auto as = juce::ARAHostModel::AudioSource::getEmptyProperties();
            as.name = "hostcheck voice";
            as.persistentID = "hostcheck-source";
            as.sampleCount = (ARA::ARASampleCount) voice.samples.size();
            as.sampleRate = voice.rate;
            as.channelCount = 1;
            as.merits64BitSamples = false;
            audioSource = std::make_unique<juce::ARAHostModel::AudioSource> (hostRef<ARA::ARAAudioSourceHostRef> (&voice), dc, as);

            auto am = juce::ARAHostModel::AudioModification::getEmptyProperties();
            am.name = "Vocal take";
            am.persistentID = "hostcheck-modification";
            audioModifications.push_back (std::make_unique<juce::ARAHostModel::AudioModification> (
                hostRef<ARA::ARAAudioModificationHostRef> (&modificationRef), dc, *audioSource, am));

            auto pr = juce::ARAHostModel::PlaybackRegion::getEmptyProperties();
            pr.transformationFlags = ARA::kARAPlaybackTransformationNoChanges;
            pr.startInModificationTime = 0.0;
            pr.durationInModificationTime = firstRegionSec;
            pr.startInPlaybackTime = 0.0;
            pr.durationInPlaybackTime = firstRegionSec;   // 準備の後に足すリージョン（40 秒から）と重ならない長さ
            pr.musicalContextRef = musicalContext->getPluginRef();
            pr.regionSequenceRef = regionSequence->getPluginRef();
            pr.name = "Vocal take";
            playbackRegions.push_back (std::make_unique<juce::ARAHostModel::PlaybackRegion> (
                hostRef<ARA::ARAPlaybackRegionHostRef> (&regionRef), dc, *audioModifications.back(), pr));
        }

        audioSource->enableAudioSourceSamplesAccess (true);

        const auto roles = ARA::kARAPlaybackRendererRole;
        extension = document->bindDocumentToPluginInstance (*instance, roles, roles);

        if (! check (extension.isValid(), "plugin instance bound to the document (playback renderer)"))
            return finish();

        playbackRenderer = extension.getPlaybackRendererInterface();   // 持っている間だけ登録が続く（RAII）
        playbackRenderer.add (*playbackRegions.front());
        instance->setPlayConfigDetails (0, 1, voice.rate, blockSize);
        instance->setPlayHead (&playHead);
        instance->prepareToPlay (voice.rate, blockSize);

        // 位置: 1・5 秒（最初から）、addedRegionStartSec + 2 秒（準備の後のリージョン）、10 秒（アクセスを切って戻す）、20 秒（内容の更新）。
        // 修正前のコードでそれぞれが別の理由で落ちるように、互いに影響しにくい順（リージョンの追加 → アクセス → 内容）に並べる
        steps = {
            { "playback at prepare (1 s)", {}, 1.0, true, 0 },
            { "region added after prepare: it plays", [this] { addRegionAfterPrepare(); }, addedRegionStartSec + 2.0, true, 0 },
            { "samples access off: read 10 s while access is off", [this] { audioSource->enableAudioSourceSamplesAccess (false); }, 10.0, false, 700 },
            { "samples access back on: 10 s plays (blocks that could not be read are read again)",
              [this] { audioSource->enableAudioSourceSamplesAccess (true); }, 10.0, true, 0 },
            { "source content updated by the host: 20 s plays (the invalidated reader is replaced)",
              [this]
              {
                  document->getDocumentController().updateAudioSourceContent (audioSource->getPluginRef(), nullptr,
                                                                              ARA::ContentUpdateScopes::samplesAreAffected());
              },
              20.0, true, 0 },
            { "the first region still plays (5 s)", {}, 5.0, true, 0 },
        };
        report ("document: 1 source (" + juce::String (duration, 0) + " s, " + juce::String (voice.rate) + " Hz), 1 region (0-" + juce::String (firstRegionSec, 0) + " s), steps " + juce::String ((int) steps.size()));
        beginStep();
        startTimer (30);
    }

    void beginStep()
    {
        stepStart = juce::Time::getMillisecondCounter();
        const auto& step = steps[(size_t) stepIndex];

        if (step.action != nullptr)
            step.action();
    }

    /** 位置 sec から 1 ブロックを描いて、最大の絶対値を返す。 */
    float renderPeak (double sec)
    {
        playHead.samples = (juce::int64) (sec * voiceRate);
        output.clear();
        midi.clear();
        instance->processBlock (output, midi);
        return output.getMagnitude (0, output.getNumSamples());
    }

    void timerCallback() override
    {
        const auto& step = steps[(size_t) stepIndex];
        const auto elapsed = (int) (juce::Time::getMillisecondCounter() - stepStart);
        const auto peak = renderPeak (step.probeSec);
        bool done = false;

        if (step.expectAudio)
        {
            if (peak > audibleThreshold)
            {
                check (true, step.name + " (peak " + juce::String (peak, 3) + " after " + juce::String (elapsed) + " ms)");
                done = true;
            }
            else if (elapsed >= stepTimeoutMs)
            {
                check (false, step.name + " (silent for " + juce::String (elapsed) + " ms)");
                done = true;
            }
        }
        else if (elapsed >= step.settleMs)
        {
            report ("step: " + step.name + " (peak " + juce::String (peak, 3) + ")");
            done = true;
        }

        if (! done)
            return;

        if (++stepIndex < (int) steps.size())
            return beginStep();

        stopTimer();
        finish();
    }

    juce::String readLog() const { return logFile.loadFileAsString(); }

    void finish()
    {
        stopTimer();

        if (instance != nullptr)
        {
            auto log = readLog();
            check (log.contains ("renderer: prepare 44100"), "plugin.log has the prepare line");
            check (log.contains ("renderer: source reader replaced id=hostcheck-source reason=samples-access"),
                   "plugin.log has the reader replacement after samples access came back");
            check (log.contains ("renderer: source reader replaced id=hostcheck-source reason=content"),
                   "plugin.log has the reader replacement after the content update");
            check (log.contains ("renderer: regions synced, 2 of 2 region(s) readable"), "plugin.log has the region sync after the region was added");

            instance->releaseResources();
            released = true;
            log = readLog();
            check (log.contains ("renderer: release blocks=") && log.contains ("unknownRegionBlocks=") && log.contains ("unpreparedBlocks=")
                       && log.contains ("incompleteReads=") && log.contains ("lockMisses=") && log.contains ("readerRefreshes="),
                   "plugin.log has the release line with the counters");
            report ("log: " + logFile.getFileName() + " (" + juce::String (log.length()) + " chars)");
        }

        releaseAll();
        report ("teardown: document released");

        if (onFinished != nullptr)
            std::exchange (onFinished, nullptr)();
    }

    void releaseAll()
    {
        if (instance != nullptr && ! released)
            instance->releaseResources();

        playbackRenderer = {};   // インスタンスを準備していない間に外す
        extension = {};

        if (instance != nullptr)
            instance->setPlayHead (nullptr);

        instance.reset();
        playbackRegions.clear();
        audioModifications.clear();
        audioSource.reset();
        regionSequence.reset();
        musicalContext.reset();
        document.reset();
        factory = {};
    }

    static constexpr double voiceRate = 44100.0;
    static constexpr int blockSize = 512;
    static constexpr float audibleThreshold = 0.05f;
    static constexpr double addedRegionStartSec = 40.0;
    static constexpr double firstRegionSec = 30.0;

    juce::DynamicLibrary keepLoaded;   // 最後に手放す（他のメンバーより先に宣言）
    Report report;
    Check check;
    juce::File plugin, logFile;
    int stepTimeoutMs = 4000;
    bool released = false;
    std::function<void()> onFinished;
    Voice voice;
    PlayHead playHead;
    juce::AudioBuffer<float> output { 1, blockSize };
    juce::MidiBuffer midi;
    std::vector<Step> steps;
    int stepIndex = 0;
    juce::uint32 stepStart = 0;

    juce::AudioPluginFormatManager formatManager;
    std::unique_ptr<juce::AudioPluginInstance> instance;
    juce::ARAFactoryWrapper factory;
    std::unique_ptr<juce::ARAHostDocumentController> document;
    std::unique_ptr<juce::ARAHostModel::MusicalContext> musicalContext;
    int sequenceRef = 0, modificationRef = 0, regionRef = 0, addedModificationRef = 0, addedRegionRef = 0;
    std::unique_ptr<juce::ARAHostModel::RegionSequence> regionSequence;
    std::unique_ptr<juce::ARAHostModel::AudioSource> audioSource;
    std::vector<std::unique_ptr<juce::ARAHostModel::AudioModification>> audioModifications;
    std::vector<std::unique_ptr<juce::ARAHostModel::PlaybackRegion>> playbackRegions;
    juce::ARAHostModel::PlugInExtensionInstance extension;
    juce::ARAHostModel::PlaybackRendererInterface playbackRenderer;
};
