#include "GlissDocumentController.h"

#include <cstring>

#include "Diagnostics.h"
#include "GlissEditorRenderer.h"
#include "GlissPlaybackRenderer.h"
#include "ara/ArchiveIO.h"
#include "ara/EngineCalls.h"
#include "ara/PluginState.h"

namespace gliss
{

namespace
{
juce::var object (std::initializer_list<std::pair<juce::Identifier, juce::var>> properties)
{
    auto* o = new juce::DynamicObject();
    for (const auto& [key, value] : properties)
        o->setProperty (key, value);
    return juce::var (o);
}

juce::String env (const char* name)
{
    return juce::SystemStats::getEnvironmentVariable (name, {});
}

juce::String regionId (const juce::ARAPlaybackRegion* region)
{
    return "r" + juce::String::toHexString ((juce::pointer_sized_int) region);
}

RegionTimes timesOf (const juce::ARAPlaybackRegion* region)
{
    RegionTimes t;
    t.id = regionId (region);
    t.songStart = region->getStartInPlaybackTime();
    t.songEnd = region->getEndInPlaybackTime();
    t.modStart = region->getStartInAudioModificationTime();
    t.modEnd = region->getEndInAudioModificationTime();
    t.normalize (region->isTimestretchEnabled());
    return t;
}
} // namespace

//==============================================================================
/** 同期のスレッドがホストの音を読む口。ホストのリーダー（ARAAudioSourceReader）はメッセージスレッドが作り・壊し、
    差し替える前にここから外す（読んでいる最中なら、読み終わるのを待ってから外す）。 */
class GlissDocumentController::AraSourceSamples final : public SourceSamples
{
public:
    bool read (float* const* dest, int numChannels, juce::int64 start, int numSamples) override
    {
        std::lock_guard guard (mutex);

        if (reader == nullptr || ! reader->isValid())
            return false;

        return reader->read (dest, numChannels, start, numSamples);
    }

    void setReader (juce::ARAAudioSourceReader* newReader)
    {
        std::lock_guard guard (mutex);
        reader = newReader;
    }

private:
    std::mutex mutex;
    juce::ARAAudioSourceReader* reader = nullptr;
};

//==============================================================================
#if GLISS_TEST_HOOKS
/** 試験用（GLISS_TEST_BRIDGE_DIR。CMake の GLISS_TEST_HOOKS のビルドだけ）: 画面の engineCall の代わり。フォルダの <名前>.call.json（{tool, args}）を順に
    engineCall に渡し、答えを <名前>.result.json に書く（plugin/tests/aratest の GlissARATest -changes が使う）。tool が "@preview" のときは
    画面の preview の代わり（{tool: "@preview", op, args}。GlissHostCheck --ara-preview が使う）。
    args の文字列 "@ara:<修飾の persistentID>" はその修飾のトラックの id に置き換える。メッセージスレッドで動く。 */
class GlissDocumentController::TestBridge final : private juce::Timer
{
public:
    TestBridge (GlissDocumentController& ownerIn, juce::File dirIn) : owner (ownerIn), dir (std::move (dirIn))
    {
        startTimer (50);
    }

private:
    void timerCallback() override
    {
        if (*busy)
            return;

        auto calls = dir.findChildFiles (juce::File::findFiles, false, "*.call.json");

        if (calls.isEmpty())
            return;

        calls.sort();
        const auto call = calls[0];
        const auto base = call.getFileName().upToFirstOccurrenceOf (".call.json", false, false);
        const auto request = juce::JSON::parse (call.loadFileAsString());
        call.deleteFile();

        auto args = request.getProperty ("args", {});

        if (auto* o = args.getDynamicObject())
            for (auto& p : o->getProperties())
                if (p.value.isString() && p.value.toString().startsWith ("@ara:"))
                    o->setProperty (p.name, owner.sync->getModStatus (p.value.toString().fromFirstOccurrenceOf ("@ara:", false, false)).trackId);

        *busy = true;
        auto answer = [out = dir.getChildFile (base + ".result.json"), tmp = dir.getChildFile (base + ".result.tmp"), flag = busy]
                      (const juce::var& result)
        {
            tmp.replaceWithText (juce::JSON::toString (result, true));
            tmp.moveFileTo (out);
            *flag = false;
        };
        const auto tool = request.getProperty ("tool", {}).toString();

        // "@preview": 画面の preview（{op, args}）の代わり。args の requester（試聴を求めたエディタの EditorRenderer の id）は、
        // 本物の GlissEditor が足すもの（EditorWebView の preview）と同じ名前
        if (tool == "@preview")
            owner.preview (request.getProperty ("op", "start").toString(), args, std::move (answer));
        else
            owner.engineCall (tool, args, std::move (answer));
    }

    GlissDocumentController& owner;
    juce::File dir;
    std::shared_ptr<bool> busy = std::make_shared<bool> (false);
};
#else
class GlissDocumentController::TestBridge {};   // 配布のビルド: 試験用の口は無い
#endif

//==============================================================================
GlissAudioModification::GlissAudioModification (juce::ARAAudioSource* audioSource,
                                                ARA::ARAAudioModificationHostRef hostRef,
                                                const juce::ARAAudioModification* optionalModificationToClone)
    : ARAAudioModification (audioSource, hostRef, optionalModificationToClone),
      editedPcm (std::make_shared<EditedPcm> (audioSource->getSampleRate(), juce::jmax (1, (int) audioSource->getChannelCount())))
{
    if (optionalModificationToClone != nullptr)
        cloneSourceId = juce::String (optionalModificationToClone->getPersistentID());
}

//==============================================================================
GlissDocumentController::GlissDocumentController (const ARA::PlugIn::PlugInEntry* entry,
                                                  const ARA::ARADocumentControllerHostInstance* instance)
    : ARADocumentControllerSpecialisation (entry, instance),
      workKey (archive::makeWorkKey())
{
    DocumentSync::Options options;
    options.engineDisabled = env ("GLISS_ENGINE_DISABLED").isNotEmpty() && env ("GLISS_ENGINE_DISABLED") != "0";
    options.testEdit = TestEdit::parse (env ("GLISS_TEST_EDIT"));
#if GLISS_TEST_HOOKS
    // 試験用: 1 回の ara_render_dirty で再合成する長さ（GlissARATest -changes が再合成を何回かに分けるのに使う）
    if (const auto maxSec = env ("GLISS_TEST_MAX_RENDER_SEC").getDoubleValue(); maxSec > 0.0)
        options.maxRenderSec = maxSec;
#endif

    if (env ("GLISS_TEST_EDIT").isNotEmpty() && ! options.testEdit.has_value())
        diag::log ("document: GLISS_TEST_EDIT could not be read: " + env ("GLISS_TEST_EDIT"));

    DocumentSync::Callbacks callbacks;
    callbacks.event = [this, token = std::weak_ptr<bool> (alive)] (const juce::String& name, const juce::var& data)
    {
        juce::MessageManager::callAsync ([this, token, name, data]
        {
            if (token.lock() != nullptr)
                onSyncEvent (name, data);
        });
    };
    callbacks.contentChanged = [this, token = std::weak_ptr<bool> (alive)] (const juce::StringArray& ids, bool notifyHost)
    {
        juce::MessageManager::callAsync ([this, token, ids, notifyHost]
        {
            if (token.lock() != nullptr)
                notifyContentChanged (ids, notifyHost);
        });
    };
    callbacks.notesChanged = [this, token = std::weak_ptr<bool> (alive)] (const juce::StringArray& ids, const juce::StringArray& sources,
                                                                            bool firstContent)
    {
        juce::MessageManager::callAsync ([this, token, ids, sources, firstContent]
        {
            if (token.lock() != nullptr)
                notifyNotesChanged (ids, sources, firstContent);
        });
    };
    callbacks.stateChanged = [this, token = std::weak_ptr<bool> (alive)] (const juce::StringArray& ids, bool documentData)
    {
        juce::MessageManager::callAsync ([this, token, ids, documentData]
        {
            if (token.lock() != nullptr)
                notifyStateChanged (ids, documentData);
        });
    };
    callbacks.log = [] (const juce::String& line) { diag::log (line); };

    const auto disabled = options.engineDisabled;
    sync = std::make_unique<DocumentSync> (EngineConfig::discover(), std::move (options), std::move (callbacks));
    diag::log ("document: created, work key " + workKey + (disabled ? " (engine disabled)" : ""));

#if GLISS_TEST_HOOKS
    if (const auto dir = env ("GLISS_TEST_BRIDGE_DIR"); dir.isNotEmpty() && juce::File::isAbsolutePath (dir))
    {
        juce::File (dir).createDirectory();
        testBridge = std::make_unique<TestBridge> (*this, juce::File (dir));
        diag::log ("document: test bridge on " + dir);
    }
#endif
}

GlissDocumentController::~GlissDocumentController()
{
    testBridge.reset();
    stopTimer();
    *alive = false;
    sync->shutdown();
    auditionPool.removeAllJobs (true, 10000);
    bridgePool.removeAllJobs (true, 10000);
    sync.reset();

    for (auto& [source, entry] : sourceEntries)
        if (entry.samples != nullptr)
            entry.samples->setReader (nullptr);

    sourceEntries.clear();
    retiredReaders.clear();
}

//==============================================================================
void GlissDocumentController::willBeginEditing (juce::ARADocument*)
{
    processBlockLock.enterWrite();
    editing = true;
}

void GlissDocumentController::didEndEditing (juce::ARADocument*)
{
    editing = false;
    processBlockLock.exitWrite();

    if (! hostLogged)
    {
        // 保存するものだけが変わったときの知らせの届き方（notifyStateChanged）。ホストの ARA の版と、文書の知らせ
        // （notifyDocumentDataChanged。ARA 2.3）を受ける口があるか
        hostLogged = true;
        auto* dc = getDocumentController();
        auto* updates = dc->getHostModelUpdateController();
        const auto documentData = updates != nullptr
            && updates->getInterface().implements<ARA_STRUCT_MEMBER (ARAModelUpdateControllerInterface, notifyDocumentDataChanged)>();
        diag::log ("document: host ARA API generation " + juce::String ((int) dc->getUsedApiGeneration())
                   + ", model updates " + (updates != nullptr ? "yes" : "no")
                   + ", document data notification " + (documentData ? "yes" : "no"));
    }

    pushModel();
}

void GlissDocumentController::willDestroyDocument (juce::ARADocument*)
{
    stopPreviewAudio();
    // 先に同期とエンジンを止める（この後、モデルの各オブジェクトが壊される）。
    sync->shutdown();
}

void GlissDocumentController::didEnableAudioSourceSamplesAccess (juce::ARAAudioSource* source, bool enable)
{
    // 切っている間に読めなかった区間を、先読みのリーダーが無音のまま持っている。戻ったら作り直す
    if (enable)
        scheduleReaderRefresh (source, "samples-access");
    else
        diag::logAlways ("source: samples access disabled id=" + juce::String (source->getPersistentID()));

    if (! editing)
        pushModel();
}

void GlissDocumentController::didUpdateAudioSourceProperties (juce::ARAAudioSource* source)
{
    scheduleReaderRefresh (source, "properties");
}

void GlissDocumentController::scheduleReaderRefresh (juce::ARAAudioSource* source, const char* reason)
{
    // 同じソースへの予約はまとめる（プロパティの更新より、内容・アクセスの理由を優先する。前者は変わっていなければ何もしない）
    const auto [pending, inserted] = pendingReaderRefresh.emplace (source, reason);

    if (! inserted && std::strcmp (pending->second, "properties") == 0)
        pending->second = reason;

    if (readerRefreshScheduled)
        return;

    readerRefreshScheduled = true;
    juce::MessageManager::callAsync ([this, token = std::weak_ptr<bool> (alive)]
    {
        if (token.lock() != nullptr)
            runReaderRefresh();
    });
}

void GlissDocumentController::runReaderRefresh()
{
    readerRefreshScheduled = false;
    auto pending = std::move (pendingReaderRefresh);
    pendingReaderRefresh.clear();

    auto* document = getDocument();

    if (document == nullptr)
        return;

    const auto& liveSources = document->getAudioSources();

    for (const auto& [source, reason] : pending)
    {
        // 予約の間に壊れたソースには触らない
        if (std::find (liveSources.begin(), liveSources.end(), source) == liveSources.end())
            continue;

        // サンプルへのアクセスが切れている間は作り直しても読めない。戻ったとき（samples-access）に作り直す
        if (! source->isSampleAccessEnabled())
            continue;

        for (auto* renderer : getDocumentController()->getPlaybackRenderers<GlissPlaybackRenderer>())
            renderer->refreshSource (source, reason);
    }
}

void GlissDocumentController::doUpdateAudioSourceContent (juce::ARAAudioSource* source, juce::ARAContentUpdateScopes scopeFlags)
{
    if (! scopeFlags.affectSamples())
        return;

    // DAW 側で音が変わった: 再生の原音のリーダーを作り直す（ARAAudioSourceReader はこの知らせで無効になる）
    scheduleReaderRefresh (source, "content");

    // 読み直す（エンジンは音の中身で比べ、違えば source_changed）。
    if (auto it = sourceEntries.find (source); it != sourceEntries.end())
        it->second.contentChanged = true;

    if (! editing)
        pushModel();
}

void GlissDocumentController::willDestroyAudioSource (juce::ARAAudioSource* source)
{
    dropSourceEntry (source);
}

void GlissDocumentController::dropSourceEntry (juce::ARAAudioSource* source)
{
    const auto it = sourceEntries.find (source);

    if (it == sourceEntries.end())
        return;

    if (it->second.samples != nullptr)
        it->second.samples->setReader (nullptr);

    // リーダーもこのソースの聞き手なので、聞き手を呼んでいる最中に壊さない（次に壊す）。
    if (it->second.reader != nullptr)
        retiredReaders.push_back (std::move (it->second.reader));

    sourceEntries.erase (it);
}

void GlissDocumentController::ensureReader (juce::ARAAudioSource* source, SourceEntry& entry)
{
    if (entry.samples == nullptr)
        entry.samples = std::make_shared<AraSourceSamples>();

    if (entry.reader != nullptr && entry.reader->isValid() && ! entry.contentChanged)
        return;

    auto fresh = std::make_unique<juce::ARAAudioSourceReader> (source);
    entry.samples->setReader (fresh.get());

    if (entry.reader != nullptr)
        retiredReaders.push_back (std::move (entry.reader));

    entry.reader = std::move (fresh);
    entry.contentChanged = false;
    ++entry.generation;
}

void GlissDocumentController::pushModel()
{
    auto* document = getDocument();

    if (document == nullptr)
        return;

    retiredReaders.clear();

    SyncModel model;
    model.workKey = workKey;
    model.documentName = juce::convertOptionalARAString (document->getName(), "Gliss");

    std::vector<TrackView> views;

    for (auto* source : document->getAudioSources<juce::ARAAudioSource>())
    {
        if (source->isDeactivatedForUndoHistory())
            continue;

        auto& entry = sourceEntries[source];
        ensureReader (source, entry);

        SyncSource s;
        s.id = juce::String (source->getPersistentID());
        s.sampleRate = source->getSampleRate();
        s.numChannels = (int) source->getChannelCount();
        s.numSamples = source->getSampleCount();
        s.generation = entry.generation;
        s.samplesAvailable = source->isSampleAccessEnabled();
        s.samples = entry.samples;
        model.sources.push_back (std::move (s));

        for (auto* modification : source->getAudioModifications<GlissAudioModification>())
        {
            if (modification->isDeactivatedForUndoHistory())
                continue;

            SyncModification m;
            m.araId = juce::String (modification->getPersistentID());
            m.sourceId = juce::String (source->getPersistentID());
            m.name = juce::convertOptionalARAString (modification->getName(), juce::convertOptionalARAString (source->getName(), "Audio"));
            m.cloneOf = modification->getCloneSourceId();
            m.pcm = modification->getEditedPcm();

            const juce::ARAPlaybackRegion* representative = nullptr;

            for (auto* region : modification->getPlaybackRegions<juce::ARAPlaybackRegion>())
            {
                m.regions.push_back (timesOf (region));

                if (representative == nullptr || region->getStartInPlaybackTime() < representative->getStartInPlaybackTime())
                    representative = region;
            }

            m.offsetSec = regions::trackOffset (m.regions);

            if (representative != nullptr)
                if (auto* sequence = representative->getRegionSequence())
                    m.group = juce::convertOptionalARAString (sequence->getName());

            views.push_back ({ m.araId, m.regions });
            model.modifications.push_back (std::move (m));
        }
    }

    // 画面のリージョンの枠は hostState の tracks[].regions だけが元。変わったら（エンジンの位置が変わらなくても）知らせる。
    bool regionsChanged = views.size() != tracks.size();

    for (size_t i = 0; ! regionsChanged && i < views.size(); ++i)
    {
        const auto& a = views[i];
        const auto& b = tracks[i];
        regionsChanged = a.araId != b.araId || a.regions.size() != b.regions.size();

        for (size_t j = 0; ! regionsChanged && j < a.regions.size(); ++j)
        {
            const auto& x = a.regions[j];
            const auto& y = b.regions[j];
            regionsChanged = x.id != y.id || ! juce::exactlyEqual (x.songStart, y.songStart) || ! juce::exactlyEqual (x.songEnd, y.songEnd)
                             || ! juce::exactlyEqual (x.modStart, y.modStart) || ! juce::exactlyEqual (x.modEnd, y.modEnd);
        }
    }

    tracks = std::move (views);
    sync->setModel (std::move (model));

    if (regionsChanged)
        sendEvent ("session-changed", object ({ { "dir", sync->getWorkDir().getFullPathName() } }));
}

//==============================================================================
juce::ARAAudioModification* GlissDocumentController::doCreateAudioModification (juce::ARAAudioSource* audioSource,
                                                                                ARA::ARAAudioModificationHostRef hostRef,
                                                                                const juce::ARAAudioModification* optionalModificationToClone) noexcept
{
    return new GlissAudioModification (audioSource, hostRef, optionalModificationToClone);
}

juce::ARAPlaybackRenderer* GlissDocumentController::doCreatePlaybackRenderer() noexcept
{
    return new GlissPlaybackRenderer (getDocumentController(), *this);
}

juce::ARAEditorRenderer* GlissDocumentController::doCreateEditorRenderer() noexcept
{
    return new GlissEditorRenderer (getDocumentController(), previewAudio);
}

juce::ScopedTryReadLock GlissDocumentController::getProcessingLock()
{
    return juce::ScopedTryReadLock { processBlockLock };
}

bool GlissDocumentController::isSyncSettled() const noexcept
{
    return sync == nullptr || sync->isSettled();
}

void GlissDocumentController::notifyContentChanged (const juce::StringArray& araIds, bool notifyHost)
{
    auto* document = getDocument();

    if (document == nullptr)
        return;

    for (auto* source : document->getAudioSources<juce::ARAAudioSource>())
    {
        for (auto* modification : source->getAudioModifications<GlissAudioModification>())
        {
            if (! araIds.contains (juce::String (modification->getPersistentID())))
                continue;

            modification->notifyContentChanged (juce::ARAContentUpdateScopes::samplesAreAffected(), notifyHost);

            for (auto* region : modification->getPlaybackRegions<juce::ARAPlaybackRegion>())
                region->notifyContentChanged (juce::ARAContentUpdateScopes::samplesAreAffected(), notifyHost);
        }
    }
}

void GlissDocumentController::notifyStateChanged (const juce::StringArray& araIds, bool documentData)
{
    auto* document = getDocument();

    if (document == nullptr)
        return;

    // 音もノートも変わらない（nothingIsAffected）が、保存するもの（アーカイブ）が変わった。ARA はプラグインに、保存の状態が
    // 変わったら確実に知らせることを求める（ホストは知らせを受けたものだけを保存し直すことがある。ARAInterface.h）。
    // 修飾ごとの知らせ（ARA 2.0）と、文書の知らせ（ガイドの指定。ARA 2.3 の notifyDocumentDataChanged。ホストに口が無ければ
    // 何もしない）の両方を送る
    for (auto* source : document->getAudioSources<juce::ARAAudioSource>())
        for (auto* modification : source->getAudioModifications<GlissAudioModification>())
            if (araIds.contains (juce::String (modification->getPersistentID())))
                modification->notifyContentChanged (juce::ARAContentUpdateScopes::nothingIsAffected(), true);

    if (documentData)
        getDocumentController()->notifyDocumentDataChanged();
}

//==============================================================================
// DAW に返すノート（kARAContentTypeNotes）
namespace
{
/** 作るときに写した ARAContentNote を返すだけの content reader（ARA のスレッドで作り・読み・壊す）。 */
class NoteContentReader final : public ARA::PlugIn::ContentReader
{
public:
    explicit NoteContentReader (const std::vector<NoteEvent>& source)
    {
        events.reserve (source.size());

        for (const auto& e : source)
            events.push_back ({ e.frequency, e.pitchNumber, e.volume, e.startPosition, e.attackDuration, e.noteDuration, e.signalDuration });
    }

    ARA::ARAInt32 getEventCount() noexcept override { return (ARA::ARAInt32) events.size(); }
    const void* getDataForEvent (ARA::ARAInt32 eventIndex) noexcept override { return &events[(size_t) eventIndex]; }

private:
    std::vector<ARA::ARAContentNote> events;
};

std::optional<NoteTimeRange> rangeOf (const ARA::ARAContentTimeRange* range)
{
    if (range == nullptr)
        return {};

    return NoteTimeRange { range->start, range->duration };
}

ARA::ARAContentGrade toAra (NoteGrade g)
{
    switch (g)
    {
        case NoteGrade::adjusted: return ARA::kARAContentGradeAdjusted;
        case NoteGrade::detected: return ARA::kARAContentGradeDetected;
        case NoteGrade::initial:  break;
    }

    return ARA::kARAContentGradeInitial;
}

bool isActive (const ARA::PlugIn::AudioModification* m)
{
    return m != nullptr && ! m->isDeactivatedForUndoHistory();
}
} // namespace

std::shared_ptr<const ModificationNotes> GlissDocumentController::readyNotes (const ARA::PlugIn::AudioModification* modification) const
{
    if (! isActive (modification))
        return nullptr;

    auto n = sync->getNotes (juce::String (modification->getPersistentID()));
    return n != nullptr && n->ready ? n : nullptr;
}

std::shared_ptr<const ModificationNotes> GlissDocumentController::notesOf (const ARA::PlugIn::AudioModification* modification) const
{
    // ホストに「まだ無い」と答えたら覚える（読めるようになったときに知らせる相手。notifyNotesChanged）。
    // 答えと記録を同じロックの中で行う（同期のスレッドが写しを置いた後の知らせと、すれ違わない）
    std::lock_guard guard (hostNotesMutex);
    auto n = readyNotes (modification);

    if (n == nullptr && isActive (modification))
        hostSawNoNotes.insert ("m:" + juce::String (modification->getPersistentID()));

    return n;
}

std::shared_ptr<const ModificationNotes> GlissDocumentController::sourceNotesOf (const ARA::PlugIn::AudioSource* source) const
{
    std::lock_guard guard (hostNotesMutex);

    // 同じソースの修飾は同じ音の同じ解析を持つ。解析の済んだ最初のもの。
    for (const auto* modification : source->getAudioModifications())
        if (auto n = readyNotes (modification))
            return n;

    hostSawNoNotes.insert ("s:" + juce::String (source->getPersistentID()));
    return nullptr;
}

bool GlissDocumentController::takeHostSawNoNotes (const juce::String& key)
{
    std::lock_guard guard (hostNotesMutex);
    return hostSawNoNotes.erase (key) > 0;
}

bool GlissDocumentController::doIsAudioSourceContentAvailable (const ARA::PlugIn::AudioSource* source, ARA::ARAContentType type)
{
    return type == ARA::kARAContentTypeNotes && sourceNotesOf (source) != nullptr;
}

ARA::ARAContentGrade GlissDocumentController::doGetAudioSourceContentGrade (const ARA::PlugIn::AudioSource* source, ARA::ARAContentType type)
{
    if (type != ARA::kARAContentTypeNotes)
        return ARA::kARAContentGradeInitial;

    const auto n = sourceNotesOf (source);
    return toAra (notes::grade (n.get(), true));
}

ARA::PlugIn::ContentReader* GlissDocumentController::doCreateAudioSourceContentReader (ARA::PlugIn::AudioSource* source, ARA::ARAContentType type,
                                                                                     const ARA::ARAContentTimeRange* range)
{
    const auto n = type == ARA::kARAContentTypeNotes ? sourceNotesOf (source) : nullptr;
    return new NoteContentReader (n != nullptr ? notes::forSource (*n, rangeOf (range)) : std::vector<NoteEvent> {});
}

bool GlissDocumentController::doIsAudioModificationContentAvailable (const ARA::PlugIn::AudioModification* modification, ARA::ARAContentType type)
{
    return type == ARA::kARAContentTypeNotes && notesOf (modification) != nullptr;
}

ARA::ARAContentGrade GlissDocumentController::doGetAudioModificationContentGrade (const ARA::PlugIn::AudioModification* modification,
                                                                                  ARA::ARAContentType type)
{
    if (type != ARA::kARAContentTypeNotes)
        return ARA::kARAContentGradeInitial;

    const auto n = notesOf (modification);
    return toAra (notes::grade (n.get()));
}

ARA::PlugIn::ContentReader* GlissDocumentController::doCreateAudioModificationContentReader (ARA::PlugIn::AudioModification* modification,
                                                                                           ARA::ARAContentType type,
                                                                                           const ARA::ARAContentTimeRange* range)
{
    const auto n = type == ARA::kARAContentTypeNotes ? notesOf (modification) : nullptr;
    return new NoteContentReader (n != nullptr ? notes::forModification (*n, rangeOf (range)) : std::vector<NoteEvent> {});
}

bool GlissDocumentController::doIsPlaybackRegionContentAvailable (const ARA::PlugIn::PlaybackRegion* region, ARA::ARAContentType type)
{
    return type == ARA::kARAContentTypeNotes && notesOf (region->getAudioModification()) != nullptr;
}

ARA::ARAContentGrade GlissDocumentController::doGetPlaybackRegionContentGrade (const ARA::PlugIn::PlaybackRegion* region, ARA::ARAContentType type)
{
    if (type != ARA::kARAContentTypeNotes)
        return ARA::kARAContentGradeInitial;

    const auto n = notesOf (region->getAudioModification());
    return toAra (notes::grade (n.get()));
}

ARA::PlugIn::ContentReader* GlissDocumentController::doCreatePlaybackRegionContentReader (ARA::PlugIn::PlaybackRegion* region,
                                                                                        ARA::ARAContentType type,
                                                                                        const ARA::ARAContentTimeRange* range)
{
    const auto n = type == ARA::kARAContentTypeNotes ? notesOf (region->getAudioModification()) : nullptr;

    if (n == nullptr)
        return new NoteContentReader ({});

    return new NoteContentReader (notes::forRegion (*n, timesOf (static_cast<const juce::ARAPlaybackRegion*> (region)), rangeOf (range)));
}

bool GlissDocumentController::doIsAudioSourceContentAnalysisIncomplete (const ARA::PlugIn::AudioSource* source, ARA::ARAContentType type)
{
    if (type != ARA::kARAContentTypeNotes || sourceNotesOf (source) != nullptr)
        return false;

    // 解析が進みようのないもの（エンジンが無い・失敗した、ホストが読ませない、修飾が無い・失敗した）は「未完了」にしない
    // （ホストが解析の終わりを待ち続けないように）。解析はエンジンの裏の準備が全部の修飾に行う。
    const auto engine = sync->getEngineStatus().state;

    if (engine == "failed" || engine == "disabled" || ! source->isSampleAccessEnabled())
        return false;

    for (const auto* modification : source->getAudioModifications())
        if (isActive (modification) && sync->getModStatus (juce::String (modification->getPersistentID())).state != "failed")
            return true;

    return false;
}

void GlissDocumentController::doRequestAudioSourceContentAnalysis (ARA::PlugIn::AudioSource* source, std::vector<ARA::ARAContentType> const& types)
{
    if (std::find (types.begin(), types.end(), ARA::kARAContentTypeNotes) == types.end())
        return;

    diag::log ("notes: the host requested the analysis of '" + juce::String (source->getPersistentID()) + "'");
    sync->requestEngine();
}

void GlissDocumentController::notifyNotesChanged (const juce::StringArray& araIds, const juce::StringArray& sourceIds, bool firstContent)
{
    auto* document = getDocument();

    if (document == nullptr)
        return;

    const auto scope = juce::ARAContentUpdateScopes::notesAreAffected();

    // 初めて読めるようになったノートは、ホストが「まだ無い」と答えられたもの（読み直すのを待っている）だけに知らせる。
    // 聞かれていなければ、ホストが次に読むときにそのまま渡る。曲を開いた（アーカイブから戻した）だけで、ホストに
    // 「中身が変わった」と知らせない（ARA の notifyAudioSourceContentChanged・notifyAudioModificationContentChanged）
    const auto toHost = [this, firstContent] (const juce::String& key) { return ! firstContent || takeHostSawNoNotes (key); };

    for (auto* source : document->getAudioSources<juce::ARAAudioSource>())
    {
        const auto sourceId = juce::String (source->getPersistentID());

        if (sourceIds.contains (sourceId))
            source->notifyContentChanged (scope, toHost ("s:" + sourceId));

        for (auto* modification : source->getAudioModifications<GlissAudioModification>())
        {
            const auto araId = juce::String (modification->getPersistentID());

            if (! araIds.contains (araId))
                continue;

            const auto host = toHost ("m:" + araId);
            modification->notifyContentChanged (scope, host);

            for (auto* region : modification->getPlaybackRegions<juce::ARAPlaybackRegion>())
                region->notifyContentChanged (scope, host);
        }
    }
}

//==============================================================================
// アーカイブ（docs/ara-plugin.md の「編集の単位・保存」）。編集リストだけを書く（解析のキャッシュは作業場所に持つ）。
bool GlissDocumentController::doStoreObjectsToStream (juce::ARAOutputStream& output, const juce::ARAStoreObjectsFilter* filter) noexcept
{
    // エンジンが動いていれば最新の編集を取り直す（ara_archive はエンジンのロックを取らないので、解析の最中も待たない）。
    sync->refreshArchives (5000);

    DocumentArchive a;
    a.workKey = sync->hasOpened() ? sync->getOpenedWorkKey() : workKey;

    const auto guide = sync->getGuideForStore();
    const auto guides = sync->getGuidesForStore();
    int withEdits = 0;

    for (const auto* modification : filter->getAudioModificationsToStore<GlissAudioModification>())
    {
        const auto id = juce::String (modification->getPersistentID());
        ModificationArchive m;
        m.name = juce::convertOptionalARAString (modification->getName());
        m.archive = sync->getArchiveForStore (id);
        withEdits += m.archive.isObject() ? 1 : 0;
        a.modifications[id] = std::move (m);

        if (id == guide)
            a.guide = guide;
    }

    // トラックごとのガイドは、修飾もガイドの修飾も保存するものだけ書く（外した修飾を指す指定は残さない）。
    for (const auto& [id, guideId] : guides)
        if (a.modifications.count (id) > 0 && a.modifications.count (guideId) > 0)
            a.guides[id] = guideId;

    const auto json = archive::write (a);
    diag::log ("archive: store " + juce::String ((int) a.modifications.size()) + " modification(s), " + juce::String (withEdits)
               + " with an engine archive, " + juce::String (json.length()) + " chars");

    return output.writeString (json);
}

bool GlissDocumentController::doRestoreObjectsFromStream (juce::ARAInputStream& input, const juce::ARARestoreObjectsFilter* filter) noexcept
{
    const auto json = input.readString();

    if (input.failed())
        return false;

    DocumentArchive a;
    juce::String error;

    if (! archive::read (json, a, error))
    {
        diag::log ("archive: restore failed, " + error);
        return false;
    }

    // 作業場所の鍵: まだエンジンで開いていなければアーカイブの鍵を使う（同じ PC なら解析のキャッシュが使える）。
    if (! sync->hasOpened() && archive::isValidWorkKey (a.workKey))
        workKey = a.workKey;

    int restored = 0;

    for (const auto& [id, m] : a.modifications)
    {
        // フィルターが対応させた今の修飾（DAW のコピー・別のドキュメントへの貼り付けでは ID が変わる）。
        auto* modification = filter->getAudioModificationToRestoreStateWithID<GlissAudioModification> (id.toRawUTF8());

        if (modification == nullptr)
            continue;

        if (m.archive.isObject())
            sync->setPendingRestore (juce::String (modification->getPersistentID()), m.archive);

        ++restored;
    }

    if (a.guide.isNotEmpty())
        if (auto* guide = filter->getAudioModificationToRestoreStateWithID<GlissAudioModification> (a.guide.toRawUTF8()))
            sync->setPendingGuide (juce::String (guide->getPersistentID()));

    // トラックごとのガイド（フィルターが対応させた今の修飾の ID に直す）。guides のあるアーカイブでは、アーカイブにある
    // 修飾の指定をアーカイブのとおりにする（載っていない修飾は外す。編集をアーカイブの内容に戻すのと同じ）。
    std::map<juce::String, juce::String> restoredGuides;

    if (a.hasGuides)
        for (const auto& [id, m] : a.modifications)
            if (auto* modification = filter->getAudioModificationToRestoreStateWithID<GlissAudioModification> (id.toRawUTF8()))
                restoredGuides[juce::String (modification->getPersistentID())] = {};

    for (const auto& [id, guideId] : a.guides)
    {
        auto* modification = filter->getAudioModificationToRestoreStateWithID<GlissAudioModification> (id.toRawUTF8());
        auto* guide = filter->getAudioModificationToRestoreStateWithID<GlissAudioModification> (guideId.toRawUTF8());

        if (modification != nullptr && guide != nullptr)
            restoredGuides[juce::String (modification->getPersistentID())] = juce::String (guide->getPersistentID());
    }

    if (! restoredGuides.empty())
        sync->setPendingGuides (restoredGuides);

    // ホストが持っているガイドの指定（保存に書く指定がこれと違ってきたら知らせる）。ガイドの指定を書いていない古い
    // アーカイブは、戻し終えた後の指定をホストが持っているものとする
    {
        juce::String guide;

        if (a.guide.isNotEmpty())
            if (auto* g = filter->getAudioModificationToRestoreStateWithID<GlissAudioModification> (a.guide.toRawUTF8()))
                guide = juce::String (g->getPersistentID());

        sync->setHostGuides (restoredGuides, guide, a.hasGuides);
    }

    diag::log ("archive: restore " + juce::String ((int) a.modifications.size()) + " entr(ies), " + juce::String (restored)
               + " matched, work key " + workKey);

    if (! editing)
        pushModel();

    return true;
}

//==============================================================================
// エディタへのイベント
void GlissDocumentController::postEvent (const juce::String& name, const juce::var& data)
{
    juce::MessageManager::callAsync ([this, token = std::weak_ptr<bool> (alive), name, data]
    {
        if (token.lock() != nullptr)
            sendEvent (name, data);
    });
}

void GlissDocumentController::sendEvent (const juce::String& name, const juce::var& data)
{
    JUCE_ASSERT_MESSAGE_THREAD
    listeners.call ([&] (Listener& l) { l.documentEvent (name, data); });
}

void GlissDocumentController::onSyncEvent (const juce::String& name, const juce::var& data)
{
    if (name == "test-edit")
        return;   // 試験用の口の結果はログだけ（同期のスレッドが書いた）

    if (name == "engine" && data.getProperty ("state", {}).toString() == "ready")
    {
        // 画面で選んだピッチ検出の方式（環境変数 GLISS_F0_ESTIMATOR があればそちら）。
        const auto estimator = loadedState().getProperty ("f0Estimator", {}).toString();

        if (estimator.isNotEmpty() && env ("GLISS_F0_ESTIMATOR").isEmpty())
            bridgePool.addJob ([s = sync.get(), estimator] { s->callTool ("set_f0_estimator", object ({ { "estimator", estimator } }), 30000); });
    }

    sendEvent (name, data);
}

void GlissDocumentController::addListener (Listener* l)
{
    listeners.add (l);

    if (! isTimerRunning())
        startTimerHz (30);
}

void GlissDocumentController::removeListener (Listener* l)
{
    listeners.remove (l);

    if (listeners.isEmpty())
        stopTimer();
}

void GlissDocumentController::timerCallback()
{
    const auto position = playheadState.read();
    if (! position.valid)
        return;
    if (position.playing && ! lastHostPlaying)
    {
        stopPreviewAudio();
    }
    lastHostPlaying = position.playing;

    const auto event = describePlayhead (position);
    auto json = juce::JSON::toString (event, true);

    if (json == lastPlayheadJson)
        return;

    lastPlayheadJson = std::move (json);
    sendEvent ("playhead", event);
}

juce::var GlissDocumentController::describePlayhead() const
{
    return describePlayhead (playheadState.read());
}

juce::var GlissDocumentController::describePlayhead (const PlayheadSnapshot& position) const
{
    std::vector<std::pair<juce::String, std::vector<RegionTimes>>> list;

    for (const auto& t : tracks)
        list.emplace_back (sync->getModStatus (t.araId).trackId, t.regions);

    return playhead::describe (position, list);
}

juce::var GlissDocumentController::describeSelection() const
{
    if (! hasSelection)
        return {};

    const auto trackId = sync->getModStatus (selectionAraId).trackId;
    return object ({ { "track_id", trackId.isNotEmpty() ? juce::var (trackId) : juce::var() },
                     { "ara_id", selectionAraId },
                     { "region", selectionRegion.toVar() } });
}

void GlissDocumentController::editorVisibilityChanged (const void* view, bool showing)
{
    selectionPolicy.setViewShowing (view, showing);
    diag::logAlways ("selection: editor view " + juce::String::toHexString ((juce::pointer_sized_int) view)
                     + (showing ? " shown" : " hidden") + " (showing editors " + juce::String (selectionPolicy.showingCount()) + ")");
}

void GlissDocumentController::editorSelectionChanged (const void* view, const juce::ARAViewSelection& selection)
{
    // 選択の中身を ARA の型を使わない形に写す。リージョン・リージョン列には persistentID が無いので、同じ文書の中で変わらない
    // 識別（リージョン = regionId、リージョン列 = オブジェクトの番地）と、リージョンの修飾の persistentID を使う
    SelectionPolicy::Input in;
    in.view = view;
    in.playheadSec = playheadState.read().songSec;
    const auto toRegion = [] (const juce::ARAPlaybackRegion* r)
    {
        SelectionPolicy::Region out;
        out.id = regionId (r);
        out.songStart = r->getStartInPlaybackTime();
        out.songEnd = r->getEndInPlaybackTime();

        if (auto* m = r->getAudioModification())
            out.modification = juce::String (m->getPersistentID());

        return out;
    };
    std::map<juce::String, const juce::ARAPlaybackRegion*> byId;

    for (auto* region : selection.getPlaybackRegions<juce::ARAPlaybackRegion>())
    {
        in.regions.push_back (toRegion (region));
        byId[regionId (region)] = region;
    }

    for (auto* sequence : selection.getRegionSequences<juce::ARARegionSequence>())
    {
        SelectionPolicy::Sequence s;
        s.id = "q" + juce::String::toHexString ((juce::pointer_sized_int) sequence);

        for (auto* region : sequence->getPlaybackRegions<juce::ARAPlaybackRegion>())
        {
            s.regions.push_back (toRegion (region));
            byId[regionId (region)] = region;
        }

        in.sequences.push_back (std::move (s));
    }

    const auto before = selectionPolicy.currentModification();
    const auto decision = selectionPolicy.decide (in);

    // 実機で Studio Pro が何を送ったかを後から調べるための行（常に書く。同じ行は省く。Diagnostics::logAlways）
    juce::StringArray regionList, sequenceList;

    for (const auto& r : in.regions)
        regionList.add (r.id + "=" + (r.modification.isEmpty() ? juce::String ("-") : r.modification));

    for (const auto& q : in.sequences)
    {
        juce::StringArray members;

        for (const auto& r : q.regions)
            members.add (r.id + "=" + (r.modification.isEmpty() ? juce::String ("-") : r.modification));

        sequenceList.add (q.id + "[" + members.joinIntoString (" ") + "]");
    }

    diag::logAlways ("selection: view " + juce::String::toHexString ((juce::pointer_sized_int) view)
                     + " regions [" + regionList.joinIntoString (" ") + "] sequences [" + sequenceList.joinIntoString (" ")
                     + "] -> " + (decision.kind == SelectionPolicy::Kind::adopt ? "adopted " + decision.region.modification : juce::String ("ignored"))
                     + " (" + decision.reason + "; was " + (before.isEmpty() ? juce::String ("none") : before) + ")");

    if (decision.kind != SelectionPolicy::Kind::adopt)
        return;

    const auto found = byId.find (decision.region.id);

    if (found == byId.end())
        return;

    selectionAraId = decision.region.modification;
    selectionRegion = timesOf (found->second);
    hasSelection = true;
    sendEvent ("selection", describeSelection());
}

//==============================================================================
// DocumentBridge（エディタが使う口。メッセージスレッドから呼ばれる）
void GlissDocumentController::engineCall (const juce::String& tool, const juce::var& args, Completion done)
{
    if (tools::isForbidden (tool))
    {
        done (object ({ { "ok", false }, { "error", tool + " is not available in the DAW plug-in" }, { "tool", tool } }));
        return;
    }

    const auto requested = juce::Time::getMillisecondCounter();
    if (tool == "render_audition")
        diag::log ("audition-latency stage=request ms=" + juce::String (requested));
    // 試聴（render_audition）は、長い呼び出し（再合成・描画データ）が bridgePool を埋めていても待たせない
    auto& pool = tool == "render_audition" ? auditionPool : bridgePool;
    pool.addJob ([this, token = std::weak_ptr<bool> (alive), tool, args, done = std::move (done), requested]
    {
        const auto started = juce::Time::getMillisecondCounter();
        if (tool == "render_audition")
            diag::log ("audition-latency stage=engine-call ms=" + juce::String (started)
                       + " queueMs=" + juce::String ((int) (started - requested)));
        auto result = sync->callTool (tool, args, 300000);
        if (tool == "render_audition")
            diag::log ("audition-latency stage=engine-result ms=" + juce::String (juce::Time::getMillisecondCounter()));
        diag::log ("bridge: engineCall " + tool + (isFailure (result) ? " failed: " + failureReason (result) : juce::String (" ok"))
                   + " (" + juce::String ((int) (juce::Time::getMillisecondCounter() - started)) + " ms)");

        if (tools::shouldSyncAfter (tool, result))
            sync->requestSync();

        juce::MessageManager::callAsync ([token, done, result]
        {
            if (token.lock() != nullptr && done)
                done (result);
        });
    });
}

juce::var GlissDocumentController::loadedState()
{
    if (! pluginStateLoaded)
    {
        pluginState = pluginstate::load (pluginstate::defaultFile());
        pluginStateLoaded = true;
    }

    return pluginState;
}

juce::var GlissDocumentController::bootstrap()
{
    sync->requestEngine();
    diag::log ("bridge: bootstrap (engine " + sync->getEngineStatus().state + ")");

    const auto state = loadedState();
    const auto engine = sync->getEngineStatus();
    const bool failed = engine.state == "failed" || engine.state == "disabled";

    auto* o = new juce::DynamicObject();
    o->setProperty ("mode", "ara");
    o->setProperty ("version", JucePlugin_VersionString);
    o->setProperty ("engineReady", engine.state == "ready");
    o->setProperty ("engineError", failed ? juce::var (engine.error) : juce::var());

    for (const auto* key : { "keys", "grid", "view", "f0Estimator" })
        o->setProperty (key, state.getProperty (key, {}));

    o->setProperty ("preview", previewAudio->hasRenderer() && (bool) state.getProperty ("preview", true));
    o->setProperty ("hostCanPreview", previewAudio->hasRenderer());
    o->setProperty ("selection", describeSelection());
    o->setProperty ("compare", compare.load());
    o->setProperty ("hostCanTransport", getDocumentController()->getHostPlaybackController() != nullptr);
    o->setProperty ("fileGuide", false);
    return juce::var (o);
}

void GlissDocumentController::saveState (const juce::var& patch)
{
    pluginState = pluginstate::merge (loadedState(), patch);

    bridgePool.addJob ([state = pluginState]
    {
        if (! pluginstate::save (pluginstate::defaultFile(), state))
            diag::log ("bridge: could not write plugin-state.json");
    });
}

const GlissDocumentController::TrackView* GlissDocumentController::findTrackByTrackId (const juce::String& trackId) const
{
    const auto araId = sync->findAraIdForTrack (trackId);

    for (const auto& t : tracks)
        if (t.araId == araId && araId.isNotEmpty())
            return &t;

    return nullptr;
}

std::optional<double> GlissDocumentController::toSongSeconds (const juce::var& arg, const juce::String& secKey) const
{
    // { song_sec } / { a, b }（ソングの秒）か、{ track_id, sec } / { track_id, a, b }（そのトラックの編集の秒）。
    const auto value = arg.getProperty (juce::Identifier (secKey), {});

    if (! (value.isDouble() || value.isInt() || value.isInt64()))
        return std::nullopt;

    const auto trackId = arg.getProperty ("track_id", {}).toString();

    if (trackId.isEmpty())
        return (double) value;

    const auto* track = findTrackByTrackId (trackId);

    if (track == nullptr)
        return std::nullopt;

    return regions::modToSong (track->regions, (double) value, hasSelection && selectionAraId == track->araId ? selectionRegion.id : juce::String());
}

juce::var GlissDocumentController::transport (const juce::String& op, const juce::var& arg)
{
    auto* controller = getDocumentController()->getHostPlaybackController();

    if (controller == nullptr)
        return object ({ { "ok", false }, { "reason", "no-controller" } });

    const auto fail = [] (const juce::String& reason) { return object ({ { "ok", false }, { "reason", reason } }); };

    if (op == "play")
        controller->requestStartPlayback();
    else if (op == "stop")
        controller->requestStopPlayback();
    else if (op == "toggle")
    {
        const auto p = playheadState.read();

        if (p.valid && p.playing)
            controller->requestStopPlayback();
        else
            controller->requestStartPlayback();
    }
    else if (op == "seek")
    {
        const auto songSec = arg.hasProperty ("song_sec") ? toSongSeconds (arg, "song_sec") : toSongSeconds (arg, "sec");

        if (! songSec.has_value())
            return fail ("no-region");

        controller->requestSetPlaybackPosition (*songSec);
    }
    else if (op == "loop")
    {
        if (! arg.isObject())
        {
            controller->requestEnableCycle (false);
            return object ({ { "ok", true } });
        }

        const auto a = toSongSeconds (arg, "a");
        auto b = toSongSeconds (arg, "b");

        // 編集の秒の b がリージョンの外（終わりちょうど）でも、a と同じリージョンの続きとして直す。
        if (a.has_value() && ! b.has_value() && arg.hasProperty ("track_id"))
            b = *a + ((double) arg.getProperty ("b", 0.0) - (double) arg.getProperty ("a", 0.0));

        if (! a.has_value() || ! b.has_value() || *b <= *a)
            return fail ("no-region");

        controller->requestSetCycleRange (*a, *b - *a);
        controller->requestEnableCycle (true);
    }
    else
    {
        return fail ("unknown-op");
    }

    return object ({ { "ok", true } });
}

void GlissDocumentController::preview (const juce::String& op, const juce::var& arg, Completion done)
{
    const auto result = [] (bool ok, const juce::String& reason = {})
    {
        return object ({ { "ok", ok }, { "reason", reason } });
    };
    if (op == "stop")
    {
        stopPreviewAudio();
        done (result (true));
        return;
    }
    if (op != "start") { done (result (false, "unknown-op")); return; }

    const auto local = (bool) arg.getProperty ("local", false);

    if (local)
    {
        // 断った理由を残す（画面は断られたらエンジンで作るので、画面からは見えない）。止められた（cancelled）のは断りではない
        done = [done = std::move (done)] (juce::var answer)
        {
            const auto reason = answer.getProperty ("reason", {}).toString();

            if (! (bool) answer.getProperty ("ok", false) && reason != "cancelled")
                diag::logAlways ("preview: local refused reason=" + reason);

            done (std::move (answer));
        };
    }

    if (! previewAudio->hasRenderer()) { done (result (false, "no-editor-renderer")); return; }
    if (playheadState.read().playing) { done (result (false, "host-playing")); return; }

    if (local)
    {
        previewLocal (arg, std::move (done));
        return;
    }

    const juce::File file (arg.getProperty ("path", {}).toString());
    if (! isReadableByEditor (file) || ! file.hasFileExtension ("wav"))
    {
        done (result (false, "invalid-path"));
        return;
    }

    targetPreview (arg, "engine");

    const auto generation = previewGeneration.fetch_add (1) + 1;
    diag::log ("audition-latency stage=native-start ms=" + juce::String (juce::Time::getMillisecondCounter()));
    const auto audio = previewAudio;
    const auto cancellationEpoch = audio->getCancellationEpoch();
    auditionPool.addJob ([this, token = std::weak_ptr<bool> (alive), file, generation, cancellationEpoch, audio, done = std::move (done)]
    {
        diag::log ("audition-latency stage=decode-start ms=" + juce::String (juce::Time::getMillisecondCounter()));
        std::unique_ptr<PreviewAudio::Clip> clip;
        juce::WavAudioFormat wav;
        std::unique_ptr<juce::AudioFormatReader> reader (wav.createReaderFor (file.createInputStream().release(), true));
        if (reader != nullptr && reader->sampleRate > 0 && reader->lengthInSamples > 1
            && reader->lengthInSamples <= (juce::int64) (reader->sampleRate * maxPreviewSeconds)
            && reader->numChannels > 0 && reader->numChannels <= 2)
        {
            juce::AudioBuffer<float> decoded ((int) reader->numChannels, (int) reader->lengthInSamples);
            if (reader->read (&decoded, 0, decoded.getNumSamples(), 0, true, true))
            {
                clip = std::make_unique<PreviewAudio::Clip>();
                clip->sampleRate = reader->sampleRate;
                for (int ch = 0; ch < decoded.getNumChannels(); ++ch)
                {
                    const auto* pcm = decoded.getReadPointer (ch);
                    clip->channels.emplace_back (pcm, pcm + decoded.getNumSamples());
                }
                PreviewAudio::fadeEdges (*clip);
            }
        }

        diag::log ("audition-latency stage=decode-end ms=" + juce::String (juce::Time::getMillisecondCounter()));
        deliverPreview (token, audio, generation, cancellationEpoch, std::move (clip), "invalid-audio", done);
    });
}

void GlissDocumentController::deliverPreview (std::weak_ptr<bool> token, std::shared_ptr<PreviewAudio> audio,
                                              std::uint64_t generation, std::uint64_t cancellationEpoch,
                                              std::unique_ptr<PreviewAudio::Clip> clip, const juce::String& failure, Completion done)
{
    auto pending = std::make_shared<std::unique_ptr<PreviewAudio::Clip>> (std::move (clip));
    juce::MessageManager::callAsync ([this, token, audio, generation, cancellationEpoch, pending, failure, done]() mutable
    {
        if (token.lock() == nullptr) return;
        if (generation != previewGeneration.load() || cancellationEpoch != audio->getCancellationEpoch())
        { done (object ({ { "ok", false }, { "reason", "cancelled" } })); return; }
        if (*pending == nullptr) { done (object ({ { "ok", false }, { "reason", failure } })); return; }
        if (playheadState.read().playing) { done (object ({ { "ok", false }, { "reason", "host-playing" } })); return; }
        if (! audio->publish (std::move (*pending), cancellationEpoch))
        { done (object ({ { "ok", false }, { "reason", "host-playing" } })); return; }
        diag::log ("audition-latency stage=published ms=" + juce::String (juce::Time::getMillisecondCounter()));
        done (object ({ { "ok", true } }));
    });
}

/** ずらさない試聴（cents = 0）を、エンジンを呼ばずに、プラグインが持つ編集済みの音（EditedPcm）と DAW のソースから作って鳴らす。
    ソースの範囲を読み、編集済みの窓が重なる所を置き換え、モノラルにして公開する（エンジンの render_audition の cents = 0 と同じ中身。
    窓の中は再生と同じ PCM、窓の外は原音）。キャッシュが最新でない（同期の最中）ときは、allow_stale でなければ断る（画面がエンジンで作る）。 */
void GlissDocumentController::previewLocal (const juce::var& arg, Completion done)
{
    const auto fail = [&done] (const juce::String& reason) { done (object ({ { "ok", false }, { "reason", reason } })); };
    const auto araId = arg.getProperty ("ara_id", {}).toString();
    const double startSec = arg.getProperty ("start_sec", 0.0);
    const double endSec = arg.getProperty ("end_sec", 0.0);
    const bool allowStale = (bool) arg.getProperty ("allow_stale", false);

    if (araId.isEmpty() || ! std::isfinite (startSec) || ! std::isfinite (endSec) || ! (endSec > startSec)
        || endSec - startSec > maxPreviewSeconds || getDocument() == nullptr)
    {
        fail ("unsupported");
        return;
    }

    juce::ARAAudioSource* source = nullptr;
    GlissAudioModification* modification = nullptr;

    for (auto* candidate : getDocument()->getAudioSources<juce::ARAAudioSource>())
        for (auto* m : candidate->getAudioModifications<GlissAudioModification>())
            if (juce::String (m->getPersistentID()) == araId)
            {
                source = candidate;
                modification = m;
            }

    const auto entry = source != nullptr ? sourceEntries.find (source) : sourceEntries.end();

    if (modification == nullptr || entry == sourceEntries.end() || entry->second.samples == nullptr
        || ! source->isSampleAccessEnabled() || source->getSampleRate() <= 0.0 || source->getChannelCount() < 1)
    {
        fail ("not-cached");
        return;
    }

    if (! allowStale && sync->getModStatus (araId).state != "ready")
    {
        fail ("stale");
        return;
    }

    const auto sampleRate = source->getSampleRate();
    const auto channels = juce::jmin ((int) source->getChannelCount(), 8);
    const auto first = juce::jlimit<juce::int64> (0, source->getSampleCount(), (juce::int64) std::llround (startSec * sampleRate));
    const auto last = juce::jlimit<juce::int64> (0, source->getSampleCount(), (juce::int64) std::llround (endSec * sampleRate));

    if (last - first < 2)
    {
        fail ("unsupported");
        return;
    }

    targetPreview (arg, "local");

    const auto generation = previewGeneration.fetch_add (1) + 1;
    diag::log ("audition-latency stage=native-start ms=" + juce::String (juce::Time::getMillisecondCounter()) + " local=1");
    const auto audio = previewAudio;
    const auto cancellationEpoch = audio->getCancellationEpoch();
    auditionPool.addJob ([this, token = std::weak_ptr<bool> (alive), samples = entry->second.samples, pcm = modification->getEditedPcm(),
                          sampleRate, channels, first, last, generation, cancellationEpoch, audio, done = std::move (done)]
    {
        const auto frames = (int) (last - first);
        juce::AudioBuffer<float> raw (channels, frames);
        raw.clear();
        std::vector<float*> pointers;

        for (int ch = 0; ch < channels; ++ch)
            pointers.push_back (raw.getWritePointer (ch));

        std::unique_ptr<PreviewAudio::Clip> clip;

        if (samples->read (pointers.data(), channels, first, frames))
        {
            if (pcm != nullptr)
                if (const auto snapshot = pcm->getSnapshot(); snapshot != nullptr && snapshot->hasWindows()
                                                              && std::abs (snapshot->getSampleRate() - sampleRate) < 1.0)
                {
                    const auto& windows = snapshot->getWindows();

                    for (auto i = snapshot->findFirstWindowEndingAfter (first); i < windows.size() && windows[i].startFrame < last; ++i)
                    {
                        const auto& w = windows[i];
                        const auto from = juce::jmax (first, w.startFrame);
                        const auto to = juce::jmin (last, w.getEndFrame());

                        if (to <= from || w.getNumChannels() < 1)
                            continue;

                        for (int ch = 0; ch < channels; ++ch)
                        {
                            const auto* src = w.getReadPointer (juce::jmin (ch, w.getNumChannels() - 1));
                            std::copy (src + (from - w.startFrame), src + (to - w.startFrame), pointers[(size_t) ch] + (from - first));
                        }
                    }
                }

            clip = std::make_unique<PreviewAudio::Clip>();
            clip->sampleRate = sampleRate;
            auto& mono = clip->channels.emplace_back ((size_t) frames, 0.0f);

            for (int ch = 0; ch < channels; ++ch)
                for (int i = 0; i < frames; ++i)
                    mono[(size_t) i] += raw.getSample (ch, i) / (float) channels;

            PreviewAudio::fadeEdges (*clip);
        }

        deliverPreview (token, audio, generation, cancellationEpoch, std::move (clip), "not-cached", done);
    });
}

void GlissDocumentController::targetPreview (const juce::var& arg, const char* mode)
{
    const auto araId = arg.getProperty ("ara_id", {}).toString();
    const auto requester = (std::uint64_t) (juce::int64) arg.getProperty ("requester", 0);
    std::vector<std::uint64_t> covering;
    int renderers = 0;

    if (araId.isNotEmpty())
        for (auto* renderer : getDocumentController()->getEditorRenderers<GlissEditorRenderer>())
        {
            ++renderers;

            if (renderer->coversModification (araId))
                covering.push_back (renderer->getRendererId());
        }

    const auto previousOwner = previewAudio->getStats().owner;
    previewAudio->beginPreview (covering, requester, juce::Time::getMillisecondCounter());
    previewStarted = true;

    const auto range = arg.hasProperty ("start_sec")
        ? juce::String ((double) arg.getProperty ("start_sec", 0.0), 3) + "-" + juce::String ((double) arg.getProperty ("end_sec", 0.0), 3)
        : juce::String ("-");

    // どのトラックを選んでいて（ui-track）どのトラックを描いていたか（shown）は画面が添える。絞り込み（narrowed）は、修飾を持つ renderer
    // か、求めた側（requester）の EditorRenderer に絞れたか
    diag::logAlways ("preview: start " + juce::String (mode) + " ara=" + araId + " note=" + arg.getProperty ("note", {}).toString()
                     + " range=" + range + " cents=" + juce::String ((double) arg.getProperty ("cents", 0.0), 1)
                     + " ui-track=" + arg.getProperty ("ui_track", {}).toString() + " shown=" + arg.getProperty ("shown_track", {}).toString()
                     + " covered=" + juce::String ((int) covering.size()) + "/" + juce::String (renderers)
                     + " narrowed=" + juce::String (previewAudio->getStats().narrowed ? 1 : 0)
                     + " requester=" + juce::String ((juce::int64) requester) + " owner=" + juce::String ((juce::int64) previousOwner));
}

void GlissDocumentController::stopPreviewAudio()
{
    previewGeneration.fetch_add (1);

    if (previewStarted)
    {
        previewStarted = false;
        const auto stats = previewAudio->getStats();
        diag::logAlways ("preview: stop played-by=" + juce::String ((juce::int64) stats.playedBy) + " narrowed=" + juce::String (stats.narrowed ? 1 : 0)
                         + " handovers=" + juce::String (stats.handovers));
    }

    previewAudio->stop();
}

void GlissDocumentController::setCompare (bool on)
{
    if (compare.exchange (on) == on)
        return;

    // ホストが描画をためている（先に書き出している）ことがあるので、全部の修飾の音が変わったと知らせる。
    juce::StringArray ids;

    for (const auto& t : tracks)
        ids.add (t.araId);

    notifyContentChanged (ids);
}

juce::var GlissDocumentController::hostState()
{
    juce::Array<juce::var> list;

    for (const auto& t : tracks)
    {
        const auto status = sync->getModStatus (t.araId);
        juce::Array<juce::var> regionList;

        for (const auto& r : t.regions)
            regionList.add (r.toVar());

        list.add (object ({ { "track_id", status.trackId.isNotEmpty() ? juce::var (status.trackId) : juce::var() },
                            { "ara_id", t.araId },
                            { "regions", regionList },
                            { "cache", object ({ { "state", status.state }, { "progress", status.progress },
                                                 { "error", status.error }, { "rev", status.rev } }) } }));
    }

    const auto engine = sync->getEngineStatus();

    return object ({ { "selection", describeSelection() },
                     { "playhead", playheadState.read().valid ? describePlayhead() : juce::var() },
                     { "tracks", list },
                     { "engine", object ({ { "state", engine.state }, { "error", engine.error } }) } });
}

void GlissDocumentController::restartEngine (Completion done)
{
    sync->restartEngine();

    bridgePool.addJob ([this, token = std::weak_ptr<bool> (alive), done = std::move (done)]
    {
        const auto ok = sync->waitForEngine (60000);
        const auto error = sync->getEngineStatus().error;

        juce::MessageManager::callAsync ([token, done, ok, error]
        {
            if (token.lock() != nullptr && done)
                done (object ({ { "ok", ok }, { "error", error } }));
        });
    });
}

bool GlissDocumentController::isReadableByEditor (const juce::File& file)
{
    // 作業場所（…\work\ara\<work_key>）の下の通常のファイル・フォルダ（フォルダは reveal 用。/fs/ はエディタがフォルダを断る）。
    const auto root = sync->getWorkDir();

    if (root == juce::File() || ! (file == root || file.isAChildOf (root)) || ! (file.existsAsFile() || file.isDirectory()))
        return false;

    // ..・. を含むパス、作業場所の下のリンク（シンボリックリンク・ジャンクション）は断る。
    for (const auto& part : juce::StringArray::fromTokens (file.getFullPathName(), "\\/", ""))
        if (part == ".." || part == ".")
            return false;

    for (auto f = file; f != root && f != f.getParentDirectory(); f = f.getParentDirectory())
        if (f.isSymbolicLink())
        {
            diag::log ("bridge: refused a path through a link: " + file.getFullPathName());
            return false;
        }

    return true;
}

} // namespace gliss
