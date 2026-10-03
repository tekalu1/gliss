#include "GlissDocumentController.h"

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
    callbacks.contentChanged = [this, token = std::weak_ptr<bool> (alive)] (const juce::StringArray& ids)
    {
        juce::MessageManager::callAsync ([this, token, ids]
        {
            if (token.lock() != nullptr)
                notifyContentChanged (ids);
        });
    };
    callbacks.log = [] (const juce::String& line) { diag::log (line); };

    const auto disabled = options.engineDisabled;
    sync = std::make_unique<DocumentSync> (EngineConfig::discover(), std::move (options), std::move (callbacks));
    diag::log ("document: created, work key " + workKey + (disabled ? " (engine disabled)" : ""));
}

GlissDocumentController::~GlissDocumentController()
{
    stopTimer();
    *alive = false;
    sync->shutdown();
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
    pushModel();
}

void GlissDocumentController::willDestroyDocument (juce::ARADocument*)
{
    // 先に同期とエンジンを止める（この後、モデルの各オブジェクトが壊される）。
    sync->shutdown();
}

void GlissDocumentController::didEnableAudioSourceSamplesAccess (juce::ARAAudioSource*, bool)
{
    if (! editing)
        pushModel();
}

void GlissDocumentController::doUpdateAudioSourceContent (juce::ARAAudioSource* source, juce::ARAContentUpdateScopes scopeFlags)
{
    if (! scopeFlags.affectSamples())
        return;

    // DAW 側で音が変わった: 読み直す（エンジンは音の中身で比べ、違えば source_changed）。
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
    return new GlissEditorRenderer (getDocumentController());
}

juce::ScopedTryReadLock GlissDocumentController::getProcessingLock()
{
    return juce::ScopedTryReadLock { processBlockLock };
}

bool GlissDocumentController::isSyncSettled() const noexcept
{
    return sync == nullptr || sync->isSettled();
}

void GlissDocumentController::notifyContentChanged (const juce::StringArray& araIds)
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

            modification->notifyContentChanged (juce::ARAContentUpdateScopes::samplesAreAffected(), true);

            for (auto* region : modification->getPlaybackRegions<juce::ARAPlaybackRegion>())
                region->notifyContentChanged (juce::ARAContentUpdateScopes::samplesAreAffected(), true);
        }
    }
}

//==============================================================================
// アーカイブ（design-stage23 §4-5）。編集リストだけを書く（解析のキャッシュは作業場所に持つ）。
bool GlissDocumentController::doStoreObjectsToStream (juce::ARAOutputStream& output, const juce::ARAStoreObjectsFilter* filter) noexcept
{
    // エンジンが動いていれば最新の編集を取り直す（ara_archive はエンジンのロックを取らないので、解析の最中も待たない）。
    sync->refreshArchives (5000);

    DocumentArchive a;
    a.workKey = sync->hasOpened() ? sync->getOpenedWorkKey() : workKey;

    const auto guide = sync->getGuideForStore();
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
    if (! playheadState.read().valid)
        return;

    const auto event = describePlayhead();
    auto json = juce::JSON::toString (event, true);

    if (json == lastPlayheadJson)
        return;

    lastPlayheadJson = std::move (json);
    sendEvent ("playhead", event);
}

juce::var GlissDocumentController::describePlayhead() const
{
    std::vector<std::pair<juce::String, std::vector<RegionTimes>>> list;

    for (const auto& t : tracks)
        list.emplace_back (sync->getModStatus (t.araId).trackId, t.regions);

    return playhead::describe (playheadState.read(), list);
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

void GlissDocumentController::editorSelectionChanged (const juce::ARAViewSelection& selection)
{
    const juce::ARAPlaybackRegion* chosen = nullptr;

    for (auto* region : selection.getPlaybackRegions<juce::ARAPlaybackRegion>())
    {
        chosen = region;
        break;
    }

    // リージョンを選んでいなければ、選んだ DAW のトラックの、再生位置に近いリージョン。
    if (chosen == nullptr)
    {
        const auto songSec = playheadState.read().songSec;
        double best = std::numeric_limits<double>::max();

        for (auto* sequence : selection.getRegionSequences<juce::ARARegionSequence>())
        {
            for (auto* region : sequence->getPlaybackRegions<juce::ARAPlaybackRegion>())
            {
                const auto start = region->getStartInPlaybackTime();
                const auto end = region->getEndInPlaybackTime();
                const auto distance = songSec < start ? start - songSec : (songSec > end ? songSec - end : 0.0);

                if (distance < best)
                {
                    best = distance;
                    chosen = region;
                }
            }

            if (chosen != nullptr)
                break;
        }
    }

    if (chosen == nullptr || chosen->getAudioModification() == nullptr)
        return;

    selectionAraId = juce::String (chosen->getAudioModification()->getPersistentID());
    selectionRegion = timesOf (chosen);
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

    bridgePool.addJob ([this, token = std::weak_ptr<bool> (alive), tool, args, done = std::move (done)]
    {
        const auto started = juce::Time::getMillisecondCounter();
        auto result = sync->callTool (tool, args, 300000);
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

    // preview は省く（つかんだノートの試聴は EditorRenderer が鳴らせるまで出さない。画面は省略をオフと読む）。
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

juce::var GlissDocumentController::preview (const juce::String&, const juce::var&)
{
    // つかんだノートの試聴（EditorRenderer で DAW の出力に鳴らす）はまだ作っていない。bootstrap も preview を出さない。
    return object ({ { "ok", false }, { "reason", "unsupported" } });
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
