#pragma once

// GlissHostCheck --ara-preview-owner: 同じ修飾（ARA の AudioModification）のリージョンが複数のトラックに置かれているとき、試聴の音を足す
// インスタンスが「DAW で最後に選んだ（空でない）選択のトラック」のものになることを確かめる（docs/ara-plugin.md「試聴を足す renderer の絞り込み」）。
// エンジンは使わない。GLISS_ENGINE_DISABLED=1・GLISS_TEST_BRIDGE_DIR を付けて呼ぶ（GLISS_TEST_HOOKS のビルド）。
//
//   GlissHostCheck --ara-preview-owner <結果を書くファイル> <Gliss.vst3> <GLISS_TEST_BRIDGE_DIR と同じフォルダ> [--timeout <秒>]
//
// 背景: Studio Pro は、ソロでミュートになったトラックのインスタンスも処理する（ARA・VST3 にソロ・ミュートを知る手段は無い）。試聴を足す
// インスタンスが「最初に処理されたもの」だと、ミュートのトラックのインスタンスが足して、試聴が聞こえなくなる。
//
// ドキュメント: ソース A（440 Hz）・B（660 Hz）。修飾 modA・modB。トラック（リージョン列）A・B・C。
//   インスタンス 1: トラック A のリージョン（modA）。インスタンス 2: トラック B のリージョン（modB）。
//   インスタンス 3: トラック C のリージョン（modA を共有。同じ素材のイベントを別のトラックに置いたとき）。
//   どのインスタンスも PlaybackRenderer と EditorRenderer の両方に自分のリージョンを持つ（Studio Pro と同じ。plugin.log の by=editor）。
// 試聴（modA のノート）は止まっている間に行う。ホストはインスタンスを毎回全部処理し（ミュートのトラックの出力は捨てる）、処理する順だけ替える。
// 選択はプラグインの試験用の口の "@selection"（本物のエディタが受ける ARAViewSelection と同じ経路の editorSelectionChanged に渡す）で知らせる。
// 音が乗ったインスタンス（peak が出たもの）が、期待した 1 つだけであることを見る:
//   選択なし:           今までの決め方（modA を持つインスタンスのうち、最初に処理されたもの）
//   トラック A のリージョンを選択: どの順で処理しても A（A をソロにしたときに聞こえる）。8 回
//   空の選択が続いても: 最後の空でない選択（A）のまま
//   トラック C を選択（リージョン列で）: C
//   modA を持たないトラック B を選択: 候補が無いので、今までの決め方に戻る

#include <juce_audio_processors/juce_audio_processors.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <functional>
#include <memory>
#include <vector>

class AraPreviewOwnerCheck final : private juce::Timer
{
public:
    using Report = std::function<void (const juce::String&)>;
    using Check = std::function<bool (bool, const juce::String&)>;

    AraPreviewOwnerCheck (Report reportIn, Check checkIn, juce::File pluginIn, juce::File bridgeDirIn, int timeoutSecIn)
        : report (std::move (reportIn)), check (std::move (checkIn)), plugin (std::move (pluginIn)), bridgeDir (std::move (bridgeDirIn)),
          stepTimeoutMs (juce::jlimit (1, 60, timeoutSecIn) * 1000)
    {
        makeVoice (voiceA, 440.0);
        makeVoice (voiceB, 660.0);
    }

    ~AraPreviewOwnerCheck() override
    {
        stopTimer();
        releaseAll();
    }

    void start (std::function<void()> onFinishedIn)
    {
        onFinished = std::move (onFinishedIn);
        keepLoaded.open (plugin.getFullPathName());
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

    static void makeVoice (Voice& v, double hz)
    {
        v.rate = voiceRate;
        v.samples.resize ((size_t) (70.0 * voiceRate));

        for (size_t i = 0; i < v.samples.size(); ++i)
            v.samples[i] = (float) (0.25 * std::sin (juce::MathConstants<double>::twoPi * hz * (double) i / voiceRate));
    }

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

    class PlayHead final : public juce::AudioPlayHead
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
        juce::String name;
        std::unique_ptr<juce::AudioPluginInstance> plugin;
        juce::ARAHostModel::PlugInExtensionInstance extension;
        juce::ARAHostModel::PlaybackRendererInterface playbackRenderer;
        juce::ARAHostModel::EditorRendererInterface editorRenderer;
        juce::AudioBuffer<float> buffer { 2, blockSize };
        float peak = 0.0f;
        float peakSince = 0.0f;
    };

    /** 1 つの場面。select が空でなければ「DAW が選択を知らせる」場面（音は出さない）、そうでなければ試聴を 1 回して、音が乗ったインスタンスを見る。 */
    struct Item
    {
        juce::String name;
        std::vector<int> order;     // 試聴の間、この順に処理する（全部のインスタンスを処理する。ミュートを処理しないホストの場面は 1 つだけ）
        int expect = -1;            // 音が乗るはずのインスタンス（0 = A、1 = B、2 = C）。-1 は選択の場面
        bool isSelection = false;
        juce::StringArray regions, sequences;   // 選択の中身（リージョン名・トラック名）。両方空は「空の選択」
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

            if (index + 1 < (int) instances.size())
                return createInstance (index + 1);

            juce::createARAFactoryAsync (*instances[0].plugin, [this] (juce::ARAFactoryWrapper wrapper)
            {
                if (! check (wrapper.get() != nullptr, "ARA factory created"))
                    return finish();

                factory = std::move (wrapper);
                createDocument();
            });
        });
    }

    std::unique_ptr<juce::ARAHostModel::RegionSequence> makeSequence (int* ref, const char* name, int order)
    {
        auto rs = juce::ARAHostModel::RegionSequence::getEmptyProperties();
        rs.name = name;
        rs.orderIndex = order;
        rs.musicalContextRef = musicalContext->getPluginRef();
        return std::make_unique<juce::ARAHostModel::RegionSequence> (hostRef<ARA::ARARegionSequenceHostRef> (ref),
                                                                     document->getDocumentController(), rs);
    }

    std::unique_ptr<juce::ARAHostModel::AudioSource> makeSource (Voice& v, const char* id)
    {
        auto as = juce::ARAHostModel::AudioSource::getEmptyProperties();
        as.name = id;
        as.persistentID = id;
        as.sampleCount = (ARA::ARASampleCount) v.samples.size();
        as.sampleRate = v.rate;
        as.channelCount = 1;
        as.merits64BitSamples = false;
        return std::make_unique<juce::ARAHostModel::AudioSource> (hostRef<ARA::ARAAudioSourceHostRef> (&v),
                                                                  document->getDocumentController(), as);
    }

    std::unique_ptr<juce::ARAHostModel::AudioModification> makeModification (int* ref, juce::ARAHostModel::AudioSource& source, const char* id)
    {
        auto am = juce::ARAHostModel::AudioModification::getEmptyProperties();
        am.name = id;
        am.persistentID = id;
        return std::make_unique<juce::ARAHostModel::AudioModification> (hostRef<ARA::ARAAudioModificationHostRef> (ref),
                                                                        document->getDocumentController(), source, am);
    }

    std::unique_ptr<juce::ARAHostModel::PlaybackRegion> makeRegion (int* ref, juce::ARAHostModel::AudioModification& mod,
                                                                    juce::ARAHostModel::RegionSequence& seq, const char* name)
    {
        auto pr = juce::ARAHostModel::PlaybackRegion::getEmptyProperties();
        pr.transformationFlags = ARA::kARAPlaybackTransformationNoChanges;
        pr.startInModificationTime = 0.0;
        pr.durationInModificationTime = regionSec;
        pr.startInPlaybackTime = 0.0;
        pr.durationInPlaybackTime = regionSec;
        pr.musicalContextRef = musicalContext->getPluginRef();
        pr.regionSequenceRef = seq.getPluginRef();
        pr.name = name;
        return std::make_unique<juce::ARAHostModel::PlaybackRegion> (hostRef<ARA::ARAPlaybackRegionHostRef> (ref),
                                                                     document->getDocumentController(), mod, pr);
    }

    void createDocument()
    {
        document = juce::ARAHostDocumentController::create (std::move (factory), "GlissHostCheck preview owner",
                                                            std::make_unique<AudioAccess>(), std::make_unique<Archiving>(),
                                                            nullptr, nullptr, std::make_unique<Playback>());

        if (! check (document != nullptr, "ARA document controller created"))
            return finish();

        {
            const juce::ARAEditGuard guard (document->getDocumentController());

            auto mc = juce::ARAHostModel::MusicalContext::getEmptyProperties();
            mc.name = "Song";
            mc.orderIndex = 0;
            musicalContext = std::make_unique<juce::ARAHostModel::MusicalContext> (hostRef<ARA::ARAMusicalContextHostRef> (&voiceA),
                                                                                   document->getDocumentController(), mc);
            seqA = makeSequence (&seqRefA, "Track A", 0);
            seqB = makeSequence (&seqRefB, "Track B", 1);
            seqC = makeSequence (&seqRefC, "Track C", 2);
            sourceA = makeSource (voiceA, "owner-source-a");
            sourceB = makeSource (voiceB, "owner-source-b");
            modA = makeModification (&modRefA, *sourceA, "owner-mod-a");
            modB = makeModification (&modRefB, *sourceB, "owner-mod-b");
            regionA = makeRegion (&regionRefA, *modA, *seqA, "A");
            regionB = makeRegion (&regionRefB, *modB, *seqB, "B");
            regionC = makeRegion (&regionRefC, *modA, *seqC, "C");
        }

        sourceA->enableAudioSourceSamplesAccess (true);
        sourceB->enableAudioSourceSamplesAccess (true);

        const auto roles = ARA::kARAPlaybackRendererRole | ARA::kARAEditorRendererRole;
        juce::ARAHostModel::PlaybackRegion* regions[] = { regionA.get(), regionB.get(), regionC.get() };

        for (size_t i = 0; i < instances.size(); ++i)
        {
            auto& in = instances[i];
            in.extension = document->bindDocumentToPluginInstance (*in.plugin, roles, roles);

            if (! check (in.extension.isValid(), in.name + " bound (playback + editor renderer)"))
                return finish();

            in.playbackRenderer = in.extension.getPlaybackRendererInterface();
            in.editorRenderer = in.extension.getEditorRendererInterface();
            in.playbackRenderer.add (*regions[i]);
            in.editorRenderer.add (*regions[i]);     // Studio Pro は EditorRenderer にも同じリージョンを渡している（plugin.log の by=editor）
            in.plugin->setPlayConfigDetails (2, 2, voiceRate, blockSize);
            in.plugin->setPlayHead (&playHead);
            in.plugin->prepareToPlay (voiceRate, blockSize);
        }

        report ("document: sources A 440 Hz, B 660 Hz; instance 1 = Track A (modA), instance 2 = Track B (modB), instance 3 = Track C (modA shared);"
                " every instance has its region in both the playback renderer and the editor renderer; transport stopped");
        buildItems();
        previewStage = Stage::settle;      // 止めてから少し回して、PlayheadState を止まった状態にする
        stageStart = juce::Time::getMillisecondCounter();
        startTimer (20);
    }

    static Item preview (const juce::String& name, std::vector<int> order, int expect)
    {
        Item item;
        item.name = name;
        item.order = std::move (order);
        item.expect = expect;
        return item;
    }

    static Item selection (const juce::String& name, juce::StringArray regions, juce::StringArray sequences)
    {
        Item item;
        item.name = name;
        item.order = { 0, 1, 2 };
        item.isSelection = true;
        item.regions = std::move (regions);
        item.sequences = std::move (sequences);
        return item;
    }

    void buildItems()
    {
        const std::vector<int> aFirst { 0, 1, 2 }, cFirst { 2, 1, 0 }, bThenA { 1, 0, 2 }, bThenC { 1, 2, 0 };

        // 選択なし: 今までの決め方（modA を持つインスタンスのうち、最初に処理されたもの）。B（modA を持たない）は足さない
        items.push_back (preview ("no selection: A first", aFirst, 0));
        items.push_back (preview ("no selection: C first", cFirst, 2));
        items.push_back (preview ("no selection: B then A", bThenA, 0));
        items.push_back (preview ("no selection: B then C", bThenC, 2));

        // トラック A のリージョンを選択（A をソロにした）: どの順で処理しても A に音が乗る
        items.push_back (selection ("select region A", { "A" }, {}));
        items.push_back (preview ("region A selected: A first", aFirst, 0));
        items.push_back (preview ("region A selected: C first", cFirst, 0));
        items.push_back (preview ("region A selected: A first (again)", aFirst, 0));
        items.push_back (preview ("region A selected: C first (again)", cFirst, 0));
        items.push_back (preview ("region A selected: B then A", bThenA, 0));
        items.push_back (preview ("region A selected: B then C", bThenC, 0));
        items.push_back (preview ("region A selected: only A processed", { 0 }, 0));
        items.push_back (preview ("region A selected: only A processed (again)", { 0 }, 0));

        // 空の選択（Studio Pro は空の選択をよく送る）は、最後の空でない選択を消さない
        items.push_back (selection ("empty selection", {}, {}));
        items.push_back (preview ("after an empty selection: C first", cFirst, 0));
        items.push_back (preview ("after an empty selection: B then C", bThenC, 0));

        // トラック C をリージョン列で選択: C に音が乗る
        items.push_back (selection ("select track C (region sequence)", {}, { "Track C" }));
        items.push_back (preview ("track C selected: A first", aFirst, 2));
        items.push_back (preview ("track C selected: C first", cFirst, 2));

        // modA を持たないトラック B を選択: 候補が無いので、今までの決め方に戻る
        items.push_back (selection ("select region B (no instance of that track has modA)", { "B" }, {}));
        items.push_back (preview ("region B selected, no candidate: C first", cFirst, 2));
        items.push_back (preview ("region B selected, no candidate: A first", aFirst, 0));
    }

    //==============================================================================
    void render (Instance& in)
    {
        // 入力 = ホストがトラックの音として渡すもの（1000 Hz）。ARA の PlaybackRenderer は捨てて置き換える
        for (int ch = 0; ch < in.buffer.getNumChannels(); ++ch)
        {
            auto* d = in.buffer.getWritePointer (ch);

            for (int i = 0; i < in.buffer.getNumSamples(); ++i)
                d[i] = (float) (0.2 * std::sin (juce::MathConstants<double>::twoPi * 1000.0 * (double) (position + i) / voiceRate));
        }

        midi.clear();
        in.plugin->processBlock (in.buffer, midi);
        in.peak = in.buffer.getMagnitude (0, in.buffer.getNumSamples());
    }

    enum class Stage { start, wait, listen, stop, settle };

    void sendBridge (const juce::String& tool, const juce::String& op, juce::var args)
    {
        auto* request = new juce::DynamicObject();
        request->setProperty ("tool", tool);
        request->setProperty ("op", op);
        request->setProperty ("args", std::move (args));
        const auto base = "owner" + juce::String (++requestCounter).paddedLeft ('0', 3);
        answerFile = bridgeDir.getChildFile (base + ".result.json");
        bridgeDir.getChildFile (base + ".call.json").replaceWithText (juce::JSON::toString (juce::var (request), true));
    }

    static juce::var startArgs()
    {
        auto* o = new juce::DynamicObject();
        o->setProperty ("local", true);
        o->setProperty ("ara_id", "owner-mod-a");
        o->setProperty ("note", "n1");
        o->setProperty ("cents", 0.0);
        o->setProperty ("start_sec", 0.5);
        o->setProperty ("end_sec", 2.0);
        o->setProperty ("allow_stale", true);
        o->setProperty ("loop", true);
        return juce::var (o);
    }

    static juce::var selectionArgs (const Item& item)
    {
        juce::Array<juce::var> regions, sequences;

        for (const auto& r : item.regions) regions.add (r);
        for (const auto& s : item.sequences) sequences.add (s);

        auto* o = new juce::DynamicObject();
        o->setProperty ("regions", regions);
        o->setProperty ("sequences", sequences);
        return juce::var (o);
    }

    void tick()
    {
        const auto now = juce::Time::getMillisecondCounter();
        const auto inStage = (int) (now - stageStart);
        const auto& item = items[(size_t) itemIndex];

        for (int b = 0; b < 8; ++b)
            for (auto index : item.order)
            {
                auto& in = instances[(size_t) index];
                render (in);
                in.peakSince = juce::jmax (in.peakSince, in.peak);
            }

        const auto next = [&] (Stage s) { previewStage = s; stageStart = now; };

        switch (previewStage)
        {
            case Stage::start:
                for (auto& in : instances) in.peakSince = 0.0f;

                if (item.isSelection)
                    sendBridge ("@selection", {}, selectionArgs (item));
                else
                    sendBridge ("@preview", "start", startArgs());

                return next (Stage::wait);

            case Stage::wait:
            {
                if (! answerFile.existsAsFile())
                {
                    if (inStage < stepTimeoutMs) return;
                    check (false, item.name + ": no answer from the test bridge");
                    return finish();
                }

                const auto answer = juce::JSON::parse (answerFile.loadFileAsString());

                if (! (bool) answer.getProperty ("ok", false))
                {
                    const auto reason = answer.getProperty ("reason", answer.getProperty ("error", "")).toString();

                    if (! item.isSelection && reason == "not-cached" && ++notCachedRetries < 100)
                    {
                        pendingRetry = true;                 // ソースの読み込みを待って出し直す
                        return next (Stage::settle);
                    }

                    check (false, item.name + ": refused (" + reason + ")");
                    return finish();
                }

                if (item.isSelection)
                {
                    report ("selection: " + item.name);
                    return next (Stage::settle);
                }

                for (auto& in : instances) in.peakSince = 0.0f;
                return next (Stage::listen);
            }

            case Stage::listen:
            {
                if (inStage < 700) return;

                juce::String detail, heardBy;
                bool ok = true;

                for (size_t i = 0; i < instances.size(); ++i)
                {
                    const auto processed = std::find (item.order.begin(), item.order.end(), (int) i) != item.order.end();
                    const auto heard = processed && instances[i].peakSince > audibleThreshold;
                    const auto want = (int) i == item.expect;
                    ok = ok && heard == want;
                    detail << " | " << instances[i].name << ": "
                           << (processed ? "peak " + juce::String (instances[i].peakSince, 3) : juce::String ("not processed"));

                    if (heard)
                        heardBy << (heardBy.isEmpty() ? "" : ", ") << instances[i].name;
                }

                check (ok, item.name + ": the preview was added by " + (heardBy.isEmpty() ? juce::String ("nobody") : heardBy)
                               + " (want " + instances[(size_t) item.expect].name + ")" + detail);
                sendBridge ("@preview", "stop", {});
                return next (Stage::stop);
            }

            case Stage::stop:
                if (! answerFile.existsAsFile() && inStage < stepTimeoutMs) return;
                return next (Stage::settle);

            case Stage::settle:
                if (inStage < 300) return;

                if (! firstSettleDone) { firstSettleDone = true; return next (Stage::start); }
                if (pendingRetry) { pendingRetry = false; return next (Stage::start); }

                if (++itemIndex < (int) items.size())
                    return next (Stage::start);

                itemIndex = (int) items.size() - 1;
                report ("scenes done: " + juce::String ((int) items.size()) + " (not-cached retries " + juce::String (notCachedRetries) + ")");
                return finish();
        }
    }

    void timerCallback() override { tick(); }

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
            if (in.plugin != nullptr && ! released)
                in.plugin->releaseResources();

        released = true;

        for (auto& in : instances)
        {
            in.playbackRenderer = {};
            in.editorRenderer = {};
            in.extension = {};

            if (in.plugin != nullptr)
                in.plugin->setPlayHead (nullptr);

            in.plugin.reset();
        }

        regionA.reset(); regionB.reset(); regionC.reset();
        modA.reset(); modB.reset();
        sourceA.reset(); sourceB.reset();
        seqA.reset(); seqB.reset(); seqC.reset();
        musicalContext.reset();
        document.reset();
        factory = {};
    }

    static constexpr double voiceRate = 44100.0;
    static constexpr int blockSize = 512;
    static constexpr float audibleThreshold = 0.05f;
    static constexpr double regionSec = 30.0;

    juce::DynamicLibrary keepLoaded;
    Report report;
    Check check;
    juce::File plugin, bridgeDir, answerFile;
    int stepTimeoutMs = 4000;
    bool firstSettleDone = false, pendingRetry = false;
    std::vector<Item> items;
    int itemIndex = 0, notCachedRetries = 0;
    juce::int64 requestCounter = 0;
    Stage previewStage = Stage::settle;
    juce::uint32 stageStart = 0;
    bool released = false;
    std::function<void()> onFinished;
    Voice voiceA, voiceB;
    PlayHead playHead;
    juce::MidiBuffer midi;
    juce::int64 position = (juce::int64) (2.0 * voiceRate);

    juce::AudioPluginFormatManager formatManager;
    juce::PluginDescription description;
    std::vector<Instance> instances = [] { std::vector<Instance> v (3); v[0].name = "instance 1 (Track A)"; v[1].name = "instance 2 (Track B)"; v[2].name = "instance 3 (Track C)"; return v; }();
    juce::ARAFactoryWrapper factory;
    std::unique_ptr<juce::ARAHostDocumentController> document;
    std::unique_ptr<juce::ARAHostModel::MusicalContext> musicalContext;
    int seqRefA = 0, seqRefB = 0, seqRefC = 0, modRefA = 0, modRefB = 0, regionRefA = 0, regionRefB = 0, regionRefC = 0;
    std::unique_ptr<juce::ARAHostModel::RegionSequence> seqA, seqB, seqC;
    std::unique_ptr<juce::ARAHostModel::AudioSource> sourceA, sourceB;
    std::unique_ptr<juce::ARAHostModel::AudioModification> modA, modB;
    std::unique_ptr<juce::ARAHostModel::PlaybackRegion> regionA, regionB, regionC;
};
