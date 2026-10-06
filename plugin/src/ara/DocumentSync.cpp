#include "DocumentSync.h"

#include "ArchiveIO.h"
#include "FloatWavWriter.h"

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

juce::var failure (const juce::String& reason)
{
    return object ({ { "ok", false }, { "error", reason } });
}

/** 版 "<解析の署名>:<編集の署名>[~n]" の編集の署名。 */
juce::String editSignature (const juce::String& rev)
{
    return rev.fromFirstOccurrenceOf (":", false, false).upToFirstOccurrenceOf ("~", false, false);
}

constexpr int captureChunk = 65536;
} // namespace

DocumentSync::DocumentSync (EngineConfig config, Options optionsIn, Callbacks callbacksIn)
    : juce::Thread ("Gliss ARA sync"),
      options (std::move (optionsIn)),
      callbacks (std::move (callbacksIn)),
      engineSource (config.source.isNotEmpty() ? config.source : juce::String ("none")),
      engineExecutable (config.executable.getFileName()),
      mcp (std::move (config))
{
    if (options.engineDisabled)
        engineStatus = { "disabled", "the engine is disabled (GLISS_ENGINE_DISABLED)" };

    mcp.setExitCallback ([this]
    {
        engineDied = true;
        notify();
    });

    startThread (juce::Thread::Priority::normal);
}

DocumentSync::~DocumentSync()
{
    shutdown();
}

void DocumentSync::shutdown()
{
    if (shuttingDown.exchange (true))
        return;

    signalThreadShouldExit();
    notify();

    // 同期のスレッドはエンジンの応答を待っていることがある。エンジンを止めると待ちが外れる
    // （止めた後にスレッドが起動し直しても、次の周で止める）。
    for (int i = 0; i < 200 && isThreadRunning(); ++i)
    {
        mcp.stop();
        waitForThreadToExit (50);
    }

    stopThread (1000);
    mcp.stop();
    settled = true;
}

//==============================================================================
void DocumentSync::setModel (SyncModel newModel)
{
    {
        std::lock_guard guard (mutex);
        model = std::move (newModel);
    }
    requestSync();
}

void DocumentSync::setPendingRestore (const juce::String& araId, const juce::var& archive)
{
    {
        std::lock_guard guard (mutex);
        auto& p = pendingRestores[araId];
        p = {};
        p.archive = archive;
        p.serial = ++restoreSerial;
    }
    requestSync();
}

void DocumentSync::setPendingGuide (const juce::String& araId)
{
    {
        std::lock_guard guard (mutex);
        pendingGuide = araId;
        guidePending = true;
    }
    requestSync();
}

void DocumentSync::setPendingGuides (const std::map<juce::String, juce::String>& guides)
{
    {
        std::lock_guard guard (mutex);

        for (const auto& [id, guideId] : guides)
            pendingGuides[id] = guideId;
    }
    requestSync();
}

void DocumentSync::setHostGuides (const std::map<juce::String, juce::String>& guides, const juce::String& guide, bool known)
{
    {
        std::lock_guard guard (mutex);
        hostGuides.clear();

        for (const auto& [id, guideId] : guides)
            if (guideId.isNotEmpty())
                hostGuides[id] = guideId;

        hostGuide = guide;
        hostGuidesKnown = known;
    }
    requestSync();
}

void DocumentSync::requestSync()
{
    dirty = true;
    settled.store (false, std::memory_order_release);
    notify();
}

void DocumentSync::requestEngine()
{
    engineWanted = true;
    requestSync();
}

void DocumentSync::restartEngine()
{
    engineWanted = true;
    restartRequested = true;
    {
        std::lock_guard guard (mutex);
        if (engineStatus.state != "disabled")
            engineStatus = { "starting", {} };
    }
    requestSync();
}

bool DocumentSync::waitForEngine (int timeoutMs)
{
    const auto deadline = juce::Time::getMillisecondCounterHiRes() + timeoutMs;

    while (! shuttingDown)
    {
        const auto status = getEngineStatus();

        if (status.state == "ready")
            return true;

        if (status.state == "failed" || status.state == "disabled")
            return false;

        if (juce::Time::getMillisecondCounterHiRes() > deadline)
            return false;

        juce::Thread::sleep (20);
    }

    return false;
}

juce::var DocumentSync::callTool (const juce::String& tool, const juce::var& args, int timeoutMs)
{
    if (options.engineDisabled)
        return failure (getEngineStatus().error);

    requestEngine();

    if (! waitForEngine (60000))
    {
        const auto status = getEngineStatus();
        return failure (status.error.isNotEmpty() ? status.error : juce::String ("the engine is not ready"));
    }

    return mcp.call (tool, args.isObject() ? args : juce::var (new juce::DynamicObject()), timeoutMs);
}

DocumentSync::EngineStatus DocumentSync::getEngineStatus() const
{
    std::lock_guard guard (mutex);
    return engineStatus;
}

DocumentSync::ModStatus DocumentSync::getModStatus (const juce::String& araId) const
{
    std::lock_guard guard (mutex);
    const auto found = modStatus.find (araId);
    return found != modStatus.end() ? found->second : ModStatus {};
}

juce::String DocumentSync::findAraIdForTrack (const juce::String& trackId) const
{
    std::lock_guard guard (mutex);

    for (const auto& [araId, status] : modStatus)
        if (status.trackId == trackId)
            return araId;

    return {};
}

juce::File DocumentSync::getWorkDir() const
{
    std::lock_guard guard (mutex);
    return workDir;
}

juce::String DocumentSync::getOpenedWorkKey() const
{
    std::lock_guard guard (mutex);
    return openedKey;
}

void DocumentSync::refreshArchives (int timeoutMs)
{
    if (getEngineStatus().state != "ready")
        return;

    refreshArchivesLocked (object ({}), timeoutMs);
}

void DocumentSync::refreshArchivesLocked (const juce::var& args, int timeoutMs)
{
    // ara_archive はエンジンのロックを取らない（解析の最中も保存を止めない）。
    const auto r = mcp.call ("ara_archive", args, timeoutMs);

    if (isFailure (r))
    {
        log ("sync: ara_archive failed: " + failureReason (r));
        return;
    }

    std::lock_guard guard (mutex);

    if (auto* archives = r.getProperty ("archives", {}).getDynamicObject())
        for (const auto& p : archives->getProperties())
            latestArchives[p.name.toString()] = p.value.getProperty ("archive", {});

    takeGuidesLocked (r.getProperty ("guide", {}), r.getProperty ("guides", {}));
}

void DocumentSync::takeGuidesLocked (const juce::var& guide, const juce::var& guides)
{
    latestGuide = guide.isString() ? guide.toString() : juce::String();

    latestGuides.clear();

    if (auto* g = guides.getDynamicObject())
        for (const auto& p : g->getProperties())
            if (p.value.isString() && p.value.toString().isNotEmpty())
                latestGuides[p.name.toString()] = p.value.toString();

    // エンジンにもう載っている指定は、戻し途中として持たない（以後はエンジンの値が正。外部の AI が変えても上書きしない）
    for (auto it = pendingGuides.begin(); it != pendingGuides.end();)
    {
        const auto latest = latestGuides.find (it->first);
        const auto applied = it->second.isEmpty() ? latest == latestGuides.end()
                                                  : (latest != latestGuides.end() && latest->second == it->second);
        it = applied ? pendingGuides.erase (it) : std::next (it);
    }
}

juce::StringArray DocumentSync::guidesChangedSinceHost (const SyncModel& m, bool ready)
{
    // 保存に書くのは、文書にある修飾どうしの指定だけ（doStoreObjectsToStream）。比べるのも同じ範囲
    std::set<juce::String> present;

    for (const auto& mod : m.modifications)
        present.insert (mod.araId);

    const auto within = [&present] (const std::map<juce::String, juce::String>& g)
    {
        std::map<juce::String, juce::String> out;

        for (const auto& [id, guideId] : g)
            if (guideId.isNotEmpty() && present.count (id) > 0 && present.count (guideId) > 0)
                out[id] = guideId;

        return out;
    };

    std::lock_guard guard (mutex);
    auto stored = latestGuides;   // getGuidesForStore と同じ（戻し途中の指定を重ねる）

    for (const auto& [id, guideId] : pendingGuides)
    {
        if (guideId.isEmpty())
            stored.erase (id);
        else
            stored[id] = guideId;
    }

    const auto now = within (stored);
    const auto commonNow = guidePending ? pendingGuide : latestGuide;
    const auto common = present.count (commonNow) > 0 ? commonNow : juce::String();

    if (! hostGuidesKnown)
    {
        // ガイドの指定を書いていない古いアーカイブ: 戻し終えた後の指定を、ホストが持っているものとする
        if (ready)
        {
            hostGuides = now;
            hostGuide = common;
            hostGuidesKnown = true;
        }

        return {};
    }

    const auto host = within (hostGuides);
    const auto hostCommon = present.count (hostGuide) > 0 ? hostGuide : juce::String();
    juce::StringArray changed;

    for (const auto& [id, guideId] : now)
        if (const auto h = host.find (id); h == host.end() || h->second != guideId)
            changed.addIfNotAlreadyThere (id);

    for (const auto& [id, guideId] : host)
        if (now.count (id) == 0)
            changed.addIfNotAlreadyThere (id);

    if (common != hostCommon)
        for (const auto& id : { common, hostCommon })
            if (id.isNotEmpty())
                changed.addIfNotAlreadyThere (id);

    if (! changed.isEmpty())
    {
        hostGuides = now;
        hostGuide = common;
    }

    return changed;
}

juce::var DocumentSync::getArchiveForStore (const juce::String& araId) const
{
    std::lock_guard guard (mutex);

    if (const auto p = pendingRestores.find (araId); p != pendingRestores.end())
        return p->second.archive;

    if (const auto a = latestArchives.find (araId); a != latestArchives.end() && a->second.isObject())
        return a->second;

    return {};
}

juce::String DocumentSync::getGuideForStore() const
{
    std::lock_guard guard (mutex);
    return guidePending ? pendingGuide : latestGuide;
}

std::map<juce::String, juce::String> DocumentSync::getGuidesForStore() const
{
    std::lock_guard guard (mutex);
    auto out = latestGuides;

    for (const auto& [id, guideId] : pendingGuides)
    {
        if (guideId.isEmpty())
            out.erase (id);
        else
            out[id] = guideId;
    }

    return out;
}

std::shared_ptr<const ModificationNotes> DocumentSync::getNotes (const juce::String& araId) const
{
    std::lock_guard guard (mutex);
    const auto found = notesByMod.find (araId);
    return found != notesByMod.end() ? found->second : nullptr;
}

//==============================================================================
void DocumentSync::emit (const juce::String& name, const juce::var& data)
{
    if (callbacks.event)
        callbacks.event (name, data);
}

void DocumentSync::log (const juce::String& line)
{
    if (callbacks.log)
        callbacks.log (line);
}

void DocumentSync::setEngineState (const juce::String& state, const juce::String& error)
{
    {
        std::lock_guard guard (mutex);

        if (engineStatus.state == state && engineStatus.error == error)
            return;

        engineStatus = { state, error };
    }

    log ("sync: engine " + state + (error.isNotEmpty() ? ": " + error : juce::String()));
    emit ("engine", object ({ { "state", state }, { "error", error } }));
}

void DocumentSync::setModState (const juce::String& araId, const juce::String& state, double progress, const juce::String& error)
{
    juce::var data;

    {
        std::lock_guard guard (mutex);
        auto& s = modStatus[araId];

        // 素材違いで編集を当てていない間は、ready の代わりに mismatch のままにする。
        auto effective = state;
        juce::String effectiveError = error;

        if (state == "ready")
            if (const auto p = pendingRestores.find (araId); p != pendingRestores.end() && p->second.mismatch)
            {
                effective = "mismatch";
                effectiveError = p->second.reason;
            }

        const auto progressed = effective == "reading" && std::abs (progress - s.progress) >= 0.05;

        if (s.state == effective && s.error == effectiveError && ! progressed)
            return;

        s.state = effective;
        s.error = effectiveError;
        s.progress = progress;

        auto* o = new juce::DynamicObject();
        o->setProperty ("track_id", s.trackId.isNotEmpty() ? juce::var (s.trackId) : juce::var());
        o->setProperty ("ara_id", araId);
        o->setProperty ("state", effective);

        if (effective == "reading")
            o->setProperty ("progress", progress);

        if (effectiveError.isNotEmpty())
            o->setProperty ("error", effectiveError);

        data = juce::var (o);
    }

    emit ("cache", data);   // ロックの外で送る
}

void DocumentSync::setModTrack (const juce::String& araId, const juce::String& trackId, const juce::String& rev)
{
    std::lock_guard guard (mutex);
    auto& s = modStatus[araId];

    if (trackId.isNotEmpty())
        s.trackId = trackId;

    if (rev.isNotEmpty())
        s.rev = rev;
}

juce::var DocumentSync::call (const juce::String& tool, const juce::var& args, int timeoutMs)
{
    if (threadShouldExit() || shuttingDown)
        return failure ("shutting down");

    return mcp.call (tool, args, timeoutMs);
}

//==============================================================================
void DocumentSync::run()
{
    while (! threadShouldExit())
    {
        cycle();

        if (threadShouldExit())
            break;

        wait (options.pollMs);
    }
}

void DocumentSync::resetEngineSession()
{
    opened = false;

    for (auto& [id, a] : applied)
    {
        a.registered = false;
        a.failed = false;
        a.sourceGeneration = -1;
    }

    localRev.clear();
    notesRev.clear();   // ノートの写しは持ったまま（取り直して変わったときだけ知らせる）
    external.reset();   // 新しいエンジンの番号は 0 から

    std::lock_guard guard (mutex);

    for (auto& [id, p] : pendingRestores)
        if (! p.mismatch)
            p.attemptedGeneration = -1;
}

bool DocumentSync::openEngine (const SyncModel& m)
{
    setEngineState ("starting");
    log ("sync: starting the engine (" + engineSource + ": " + (engineExecutable.isNotEmpty() ? engineExecutable : juce::String ("not found")) + ")");

    const auto info = call ("engine_info", object ({}), 120000);

    if (threadShouldExit() || shuttingDown)
        return false;   // ドキュメントを閉じて止めた（失敗ではない）

    if (isFailure (info))
    {
        setEngineState ("failed", failureReason (info));
        return false;
    }

    auto key = m.workKey;

    if (! archive::isValidWorkKey (key))
        key = archive::makeWorkKey();

    // 一度開いた鍵は変えない（同じドキュメントのエンジンを起動し直したときも同じ作業場所）。
    if (openedOnce)
        key = getOpenedWorkKey();

    const auto r = call ("ara_open", object ({ { "work_key", key }, { "name", m.documentName } }), 120000);

    if (isFailure (r))
    {
        setEngineState ("failed", failureReason (r));
        return false;
    }

    {
        std::lock_guard guard (mutex);
        workDir = juce::File (r.getProperty ("dir", {}).toString());
        openedKey = key;
    }

    log ("sync: ara_open " + key + " -> " + workDir.getFullPathName() + " (engine " + info.getProperty ("version", {}).toString() + ")");
    opened = true;
    openedOnce = true;
    setEngineState ("ready");
    emit ("session-changed", object ({ { "dir", getWorkDir().getFullPathName() } }));
    return true;
}

bool DocumentSync::captureSource (const SyncSource& s, const SyncModel& m)
{
    const auto dir = getWorkDir().getChildFile ("ara-src");

    if (! dir.createDirectory())
        return false;

    const auto key = archive::sourceKey (s.id);
    const auto wav = dir.getChildFile (key + ".wav");
    const auto tmp = dir.getChildFile (key + ".wav.tmp");

    juce::StringArray mods;
    for (const auto& mod : m.modifications)
        if (mod.sourceId == s.id)
            mods.add (mod.araId);

    const auto report = [&] (double progress)
    {
        for (const auto& id : mods)
            setModState (id, "reading", progress);
    };

    settled.store (false, std::memory_order_release);
    report (0.0);

    FloatWavWriter writer;

    if (s.numChannels <= 0 || ! writer.open (tmp, s.sampleRate, s.numChannels))
    {
        for (const auto& id : mods)
            setModState (id, "failed", 0.0, "could not write the source audio");
        return false;
    }

    juce::AudioBuffer<float> buffer (s.numChannels, captureChunk);
    bool ok = true;

    for (juce::int64 pos = 0; pos < s.numSamples;)
    {
        if (threadShouldExit() || restartRequested || engineDied)
        {
            ok = false;
            break;
        }

        const auto n = (int) juce::jmin<juce::int64> (captureChunk, s.numSamples - pos);

        if (! s.samples->read (buffer.getArrayOfWritePointers(), s.numChannels, pos, n) || ! writer.write (buffer.getArrayOfReadPointers(), n))
        {
            ok = false;
            break;
        }

        pos += n;
        report ((double) pos / (double) s.numSamples);
    }

    ok = writer.finish() && ok;

    if (ok)
    {
        // 置き換える（moveFileTo は先に消すので、エンジンが無いファイルを見ることがある）。エンジンがちょうど読んでいると
        // 置き換えられないことがある。少し待ってやり直す。
        ok = false;

        for (int attempt = 0; attempt < 20 && ! ok; ++attempt)
        {
            ok = tmp.replaceFileIn (wav);

            if (! ok)
                juce::Thread::sleep (50);
        }
    }

    if (! ok)
    {
        tmp.deleteFile();
        log ("sync: reading source '" + s.id + "' did not finish (will retry)");
        return false;
    }

    captured[s.id] = { s.generation, true, wav };
    log ("sync: source '" + s.id + "' -> " + wav.getFileName() + " (" + juce::String (s.numSamples) + " samples, "
         + juce::String (s.numChannels) + " ch, " + juce::String (s.sampleRate) + " Hz)");
    return true;
}

bool DocumentSync::registerModification (const SyncModification& mod, const SyncSource& source, juce::StringArray& changed)
{
    const auto& c = captured[source.id];
    auto& a = applied[mod.araId];

    if (a.sourceGeneration == c.generation && (a.registered || a.failed))
        return a.registered;

    auto* args = new juce::DynamicObject();
    args->setProperty ("ara_id", mod.araId);
    args->setProperty ("source_path", c.wav.getFullPathName());
    args->setProperty ("source_id", source.id);
    args->setProperty ("name", mod.name);

    if (mod.offsetSec.has_value())
        args->setProperty ("offset_sec", *mod.offsetSec);

    if (mod.group.isNotEmpty())
        args->setProperty ("group", mod.group);

    // 複製（DAW の「固有にする」など）: 新しく作るときだけ、複製元の編集を写す。
    if (! a.everRegistered && mod.cloneOf.isNotEmpty())
        if (const auto origin = applied.find (mod.cloneOf); origin != applied.end() && origin->second.everRegistered)
            args->setProperty ("clone_of", mod.cloneOf);

    const auto r = call ("ara_set_modification", juce::var (args), 300000);

    if (isFailure (r))
    {
        // 同じ音のままなら繰り返さない（音が変わる・エンジンを起動し直すまで）。
        a.failed = true;
        a.sourceGeneration = c.generation;
        setModState (mod.araId, "failed", 0.0, failureReason (r));
        return false;
    }

    a.registered = true;
    setOpenedEdits (mod.araId, r, false);
    a.failed = false;
    a.everRegistered = true;
    a.sourceGeneration = c.generation;
    a.trackId = r.getProperty ("track", {}).getProperty ("id", {}).toString();
    a.offsetSec = mod.offsetSec;
    a.name = mod.name;
    a.group = mod.group;
    setModTrack (mod.araId, a.trackId, {});
    changed.addIfNotAlreadyThere (mod.araId);
    sessionChanged = true;

    log ("sync: ara_set_modification " + mod.araId + " -> " + a.trackId
         + ((bool) r.getProperty ("created", false) ? " (created)" : "")
         + ((bool) r.getProperty ("cloned", false) ? " (cloned)" : "")
         + ((bool) r.getProperty ("source_changed", false) ? " (source changed)" : ""));

    restoreIfPending (mod.araId, c.generation, changed);
    return true;
}

void DocumentSync::restoreIfPending (const juce::String& araId, int generation, juce::StringArray& changed)
{
    juce::var archive;
    int serial = 0;

    {
        std::lock_guard guard (mutex);
        const auto p = pendingRestores.find (araId);

        if (p == pendingRestores.end() || p->second.attemptedGeneration == generation)
            return;

        archive = p->second.archive;
        serial = p->second.serial;
    }

    const auto r = call ("ara_restore", object ({ { "ara_id", araId }, { "archive", archive } }), 300000);
    const auto trackId = applied[araId].trackId;
    bool restored = false, mismatch = false;
    juce::String reason;

    {
        std::lock_guard guard (mutex);
        const auto p = pendingRestores.find (araId);

        if (p == pendingRestores.end() || p->second.serial != serial)
            return;   // 待っている間に別のアーカイブが来た（次の周で当てる）

        if (isFailure (r))
        {
            p->second.attemptedGeneration = generation;
            reason = failureReason (r);
        }
        else if ((bool) r.getProperty ("mismatch", false))
        {
            p->second.attemptedGeneration = generation;
            p->second.mismatch = true;
            p->second.reason = r.getProperty ("reason", "the audio in the DAW is different").toString();
            p->second.editSignature = {};
            mismatch = true;
            reason = p->second.reason;
        }
        else
        {
            pendingRestores.erase (p);
            restored = true;
        }
    }

    if (restored)
    {
        setOpenedEdits (araId, r, true);
        log ("sync: ara_restore " + araId + " ok (" + r.getProperty ("edits", 0).toString() + " edit(s))"
             + ((bool) r.getProperty ("render_changed", false) ? ", saved with a newer renderer (the sound may change)" : ""));
        changed.addIfNotAlreadyThere (araId);
        emit ("project-changed", object ({ { "track_id", trackId } }));
    }
    else if (mismatch)
    {
        log ("sync: ara_restore " + araId + " mismatch: " + reason);
        setModState (araId, "mismatch", 0.0, reason);
    }
    else
    {
        log ("sync: ara_restore " + araId + " failed: " + reason);
        setModState (araId, "failed", 0.0, reason);
    }
}

void DocumentSync::setOpenedEdits (const juce::String& araId, const juce::var& result, bool restored)
{
    const auto rev = result.getProperty ("rev", {}).toString();

    if (rev.isNotEmpty())
        openedEdits[araId] = editSignature (rev);
    else
        openedEdits.erase (araId);

    // 保存の状態: 戻したらそれがホストの持っているもの。登録（し直し）では、前に覚えたものを保つ（エンジンを起動し直しても、
    // まだ知らせていない変更を「ホストが持っている」にしない）。返り値に無いエンジンでは ara_revs で最初に見たもの
    const auto state = result.getProperty ("state", {}).toString();

    if (restored)
    {
        if (state.isNotEmpty())
            hostStates[araId] = state;
        else
            hostStates.erase (araId);

        // このエンジンより新しい描画の版で保存した編集（render_changed）: 戻した状態に追いつくだけでも、保存したときと
        // 音が変わりうる（ホストに知らせる）。前の版の編集はその版のまま鳴らす（エンジンが変えない）
        if ((bool) result.getProperty ("render_changed", false))
            renderChanged.insert (araId);
        else
            renderChanged.erase (araId);
    }
    else if (state.isNotEmpty())
    {
        hostStates.emplace (araId, state);
    }
}

bool DocumentSync::renderModification (const SyncModification& mod, bool stateChanged, juce::StringArray& contentChanged,
                                       juce::StringArray& caughtUp)
{
    if (mod.pcm == nullptr)
        return true;

    auto since = localRev.count (mod.araId) != 0 ? localRev[mod.araId] : juce::String();
    // 手元に音が無い（登録・アーカイブから戻した直後・エンジンを起動し直した）か、前の版が解析待ちだった: 編集がホストの
    // 持っているもの（開いた時・最後に音の変化を知らせた時）のままで、保存の状態も変わっていなければ、その音に追いつくだけで、
    // ドキュメントは変わっていない（ホストには知らせない。曲を開いただけで「変更あり」にしない）。変わっていれば知らせる
    // （ホストが保存を求めないと編集が失われる）。エンジンが保存したときの音を出せないと言った（render_changed）編集は、
    // 追いつくだけでも音が変わりうるので知らせる
    const auto opened = openedEdits.find (mod.araId);
    const bool catchingUp = (since.isEmpty() || getModStatus (mod.araId).state == "waiting") && opened != openedEdits.end()
                            && ! stateChanged && renderChanged.count (mod.araId) == 0;

    settled.store (false, std::memory_order_release);
    setModState (mod.araId, "syncing");
    bool analysisPending = false;

    for (int i = 0; i < 10000; ++i)
    {
        if (threadShouldExit() || engineDied)
            return false;

        auto* args = new juce::DynamicObject();
        args->setProperty ("ara_id", mod.araId);
        args->setProperty ("since", since.isNotEmpty() ? juce::var (since) : juce::var());
        args->setProperty ("max_sec", options.maxRenderSec);

        const auto r = call ("ara_render_dirty", juce::var (args), 600000);
        DirtyUpdate u;
        juce::String error;

        if (! DirtyUpdate::parse (r, u, error))
        {
            localRev.erase (mod.araId);
            setModState (mod.araId, "failed", 0.0, error);
            return false;
        }

        const auto before = mod.pcm->getSnapshot();
        const bool hadWindows = before != nullptr && before->hasWindows();

        if (! juce::exactlyEqual (mod.pcm->getSampleRate(), u.sampleRate) || mod.pcm->getNumChannels() != u.numChannels)
        {
            mod.pcm->setFormat (u.sampleRate, u.numChannels);

            if (! u.reset)
            {
                // 手元の窓を捨てたので、全部を取り直す。
                since = {};
                continue;
            }
        }

        if (! mod.pcm->applyDirty (u.rev, u.reset, u.restore, u.windows, u.path))
        {
            localRev.erase (mod.araId);
            setModState (mod.araId, "failed", 0.0, "could not read " + u.path.getFileName());
            return false;
        }

        const bool quiet = catchingUp && editSignature (u.rev) == opened->second;

        // 窓の無いまま（原音のまま）の reset は音が変わらない（編集の無い修飾の解析が変わっただけ）
        if ((u.reset && hadWindows) || ! u.restore.empty() || ! u.windows.empty())
        {
            (quiet ? caughtUp : contentChanged).addIfNotAlreadyThere (mod.araId);

            // ホストに知らせた: 以後の「追いつくだけ」はこの編集と比べる（取り消して開いた時の編集に戻ったら、また知らせる）
            if (! quiet)
            {
                openedEdits[mod.araId] = editSignature (u.rev);
                renderChanged.erase (mod.araId);
            }
        }

        log ("sync: ara_render_dirty " + mod.araId + " rev " + u.rev + (u.reset ? " reset" : "") + ", "
             + juce::String ((int) u.restore.size()) + " restore, " + juce::String ((int) u.windows.size()) + " window(s)"
             + (u.analysisPending ? ", analysis pending" : "") + (u.more ? ", more" : "") + (quiet ? ", catching up" : ""));
        localRev[mod.araId] = u.rev;
        setModTrack (mod.araId, {}, u.rev);
        since = u.rev;
        analysisPending = u.analysisPending;

        if (! u.more)
            break;
    }

    mod.pcm->releaseUnusedSnapshots();
    setModState (mod.araId, analysisPending ? "waiting" : "ready");
    return ! analysisPending;
}

void DocumentSync::refreshNotes (const SyncModel& m, const std::map<juce::String, juce::String>& targets)
{
    juce::Array<juce::var> ids;

    for (const auto& [id, rev] : targets)
        ids.add (id);

    // ara_notes は解析を待たない（まだなら pending。裏の準備が済むと ara_revs の版が変わり、また取り直す）。
    const auto r = call ("ara_notes", object ({ { "ara_ids", ids } }), 120000);

    if (isFailure (r))
    {
        log ("sync: ara_notes failed: " + failureReason (r));
        return;
    }

    auto parsed = notes::parse (r);
    juce::StringArray changedMods, changedSources, firstMods, firstSources;
    juce::StringArray summary;

    for (const auto& [id, rev] : targets)
    {
        auto row = parsed.find (id);

        if (row == parsed.end())
            continue;

        // 取った版を覚える（取っている間に編集が入っていれば、次の ara_revs で違って取り直す）。
        notesRev[id] = rev;
        auto fresh = std::make_shared<const ModificationNotes> (std::move (row->second));
        std::shared_ptr<const ModificationNotes> old;

        {
            std::lock_guard guard (mutex);
            auto& slot = notesByMod[id];
            old = slot;
            slot = fresh;
        }

        // 初めて読めるようになった（それまで pending か、まだ取っていない）: ホストが「まだ無い」を見ていたものだけに
        // 知らせる（GlissDocumentController::notifyNotesChanged）。読めていたものが変わったら、いつも知らせる
        const bool first = fresh->ready && (old == nullptr || ! old->ready);
        const bool modChanged = old != nullptr ? ! old->sameContent (*fresh) : fresh->ready;
        const bool sourceChanged = old != nullptr ? ! old->sameSourceContent (*fresh) : fresh->ready;

        if (modChanged)
            (first ? firstMods : changedMods).add (id);

        if (sourceChanged)
            for (const auto& mod : m.modifications)
                if (mod.araId == id)
                    (first ? firstSources : changedSources).addIfNotAlreadyThere (mod.sourceId);

        summary.add (id + " " + (fresh->ready ? juce::String ((int) fresh->notes.size()) + (fresh->edited ? " adjusted" : " detected")
                                              : juce::String ("pending")));
    }

    log ("sync: ara_notes " + summary.joinIntoString (", ") + (changedMods.isEmpty() ? juce::String() : " (changed)")
         + (firstMods.isEmpty() ? juce::String() : " (first)"));

    if (! callbacks.notesChanged)
        return;

    if (! firstMods.isEmpty() || ! firstSources.isEmpty())
        callbacks.notesChanged (firstMods, firstSources, true);

    if (! changedMods.isEmpty() || ! changedSources.isEmpty())
        callbacks.notesChanged (changedMods, changedSources, false);
}

void DocumentSync::applyTestEdit (const SyncModel& m)
{
    if (! options.testEdit.has_value() || testEditDone || m.modifications.empty())
        return;

    const auto& first = m.modifications.front();
    const auto a = applied.find (first.araId);

    if (a == applied.end() || ! a->second.registered)
        return;

    testEditDone = true;
    const auto& edit = *options.testEdit;
    juce::var result;

    auto r = call ("select_track", object ({ { "track_id", a->second.trackId } }), 300000);

    if (! isFailure (r))
        r = call ("analyze_take", object ({ { "background", false } }), 600000);

    result = isFailure (r) ? r : call (edit.tool, edit.args, 600000);

    log ("sync: test edit " + edit.tool + " " + juce::JSON::toString (edit.args, true) + " on " + first.araId + " -> "
         + juce::JSON::toString (result, true).substring (0, 300));
    emit ("test-edit", object ({ { "ok", ! isFailure (result) }, { "ara_id", first.araId }, { "result", result } }));
}

void DocumentSync::cycle()
{
    dirty = false;
    sessionChanged = false;

    if (restartRequested.exchange (false))
    {
        mcp.stop();
        engineDied = false;
        resetEngineSession();
        engineFailed = false;
    }

    if (engineDied.exchange (false))
    {
        resetEngineSession();
        engineFailed = true;
        setEngineState ("failed", "the engine stopped");
    }

    SyncModel m;
    juce::String guideToApply;
    bool applyGuide = false;
    std::map<juce::String, juce::String> guidesToApply;

    {
        std::lock_guard guard (mutex);
        m = model;
        guideToApply = pendingGuide;
        applyGuide = guidePending;
        guidesToApply = pendingGuides;
    }

    const auto findSource = [&m] (const juce::String& id) -> const SyncSource*
    {
        for (const auto& s : m.sources)
            if (s.id == id)
                return &s;
        return nullptr;
    };

    bool needEngine = engineWanted;

    for (const auto& mod : m.modifications)
        if (const auto* s = findSource (mod.sourceId); s != nullptr && s->samplesAvailable)
            needEngine = true;

    const auto finish = [this] (bool workLeft)
    {
        settled.store (! dirty && ! workLeft, std::memory_order_release);
    };

    if (options.engineDisabled || ! needEngine || engineFailed)
    {
        finish (false);
        return;
    }

    if (! opened && ! openEngine (m))
    {
        engineFailed = true;
        finish (false);
        return;
    }

    bool workLeft = false;
    juce::StringArray changed;   // 保存用の写しを取り直す修飾

    // 1. 外した修飾
    for (auto it = applied.begin(); it != applied.end();)
    {
        const auto& araId = it->first;
        const bool present = std::any_of (m.modifications.begin(), m.modifications.end(), [&] (const auto& mod) { return mod.araId == araId; });

        if (present || ! it->second.registered)
        {
            ++it;
            continue;
        }

        const auto r = call ("ara_remove_modification", object ({ { "ara_id", araId } }), 120000);
        log ("sync: ara_remove_modification " + araId + (isFailure (r) ? " failed: " + failureReason (r) : juce::String()));
        localRev.erase (araId);
        notesRev.erase (araId);

        {
            std::lock_guard guard (mutex);
            modStatus.erase (araId);
            notesByMod.erase (araId);
        }

        it = applied.erase (it);
        sessionChanged = true;
    }

    // 2. ソースの音 → 一時 WAV → すぐ ara_set_modification（書き換えから登録までの間、エンジンはそのトラックを開かない）
    for (const auto& s : m.sources)
    {
        if (threadShouldExit() || engineDied)
            return;

        const bool used = std::any_of (m.modifications.begin(), m.modifications.end(), [&] (const auto& mod) { return mod.sourceId == s.id; });

        if (! used || ! s.samplesAvailable || s.samples == nullptr)
            continue;

        if (const auto c = captured.find (s.id); c != captured.end() && c->second.ok && c->second.generation == s.generation)
            continue;

        if (! captureSource (s, m))
        {
            workLeft = true;
            continue;
        }

        for (const auto& mod : m.modifications)
            if (mod.sourceId == s.id)
                workLeft = ! registerModification (mod, s, changed) || workLeft;
    }

    // ソースが消えたら WAV も消す（ドキュメントを閉じたときは残す）
    for (auto it = captured.begin(); it != captured.end();)
    {
        if (findSource (it->first) == nullptr)
        {
            it->second.wav.deleteFile();
            it = captured.erase (it);
        }
        else
        {
            ++it;
        }
    }

    // 3. 読み込み済みのソースに新しくできた修飾・エンジンを起動し直した後の登録し直し
    for (const auto& mod : m.modifications)
    {
        const auto* s = findSource (mod.sourceId);

        if (s == nullptr || ! s->samplesAvailable)
            continue;

        const auto c = captured.find (s->id);

        if (c == captured.end() || ! c->second.ok || c->second.generation != s->generation)
            continue;

        workLeft = ! registerModification (mod, *s, changed) || workLeft;
    }

    // 4. 位置・名前・DAW のトラック名の変化、アーカイブのガイド
    juce::Array<juce::var> tracks;

    for (const auto& mod : m.modifications)
    {
        auto a = applied.find (mod.araId);

        if (a == applied.end() || ! a->second.registered)
            continue;

        const bool offsetChanged = mod.offsetSec.has_value()
                                   && (! a->second.offsetSec.has_value() || std::abs (*a->second.offsetSec - *mod.offsetSec) > 1.0e-9);

        if (! offsetChanged && a->second.name == mod.name && a->second.group == mod.group)
            continue;

        auto* t = new juce::DynamicObject();
        t->setProperty ("ara_id", mod.araId);

        if (mod.offsetSec.has_value())
            t->setProperty ("offset_sec", *mod.offsetSec);

        t->setProperty ("name", mod.name);
        t->setProperty ("group", mod.group);
        tracks.add (juce::var (t));
        a->second.offsetSec = mod.offsetSec;
        a->second.name = mod.name;
        a->second.group = mod.group;
    }

    bool guideReady = false;

    if (applyGuide)
    {
        const auto a = applied.find (guideToApply);
        guideReady = guideToApply.isEmpty() || (a != applied.end() && a->second.registered);
    }

    // トラックごとのガイド: 修飾とガイドの修飾の両方が登録できたものだけ当てる（残りは次の同期で）。
    const auto isRegistered = [this] (const juce::String& id)
    {
        const auto a = applied.find (id);
        return a != applied.end() && a->second.registered;
    };
    std::map<juce::String, juce::String> guidesReady;

    for (const auto& [id, guideId] : guidesToApply)
        if (isRegistered (id) && (guideId.isEmpty() || isRegistered (guideId)))
            guidesReady[id] = guideId;

    if (! tracks.isEmpty() || guideReady || ! guidesReady.empty())
    {
        auto* args = new juce::DynamicObject();
        args->setProperty ("tracks", tracks);

        if (guideReady)
            args->setProperty ("guide", guideToApply);

        if (! guidesReady.empty())
        {
            auto* guides = new juce::DynamicObject();

            for (const auto& [id, guideId] : guidesReady)
                guides->setProperty (juce::Identifier (id), guideId);

            args->setProperty ("guides", juce::var (guides));
        }

        const auto r = call ("ara_sync", juce::var (args), 120000);

        if (isFailure (r))
        {
            log ("sync: ara_sync failed: " + failureReason (r));
        }
        else
        {
            sessionChanged = true;

            if (guideReady)
            {
                std::lock_guard guard (mutex);

                if (pendingGuide == guideToApply)
                    guidePending = false;
            }

            if (! guidesReady.empty())
            {
                // エンジンが答えた組は当てた・断られた（伴奏・自分自身）・知らない、のどれでも残さない。残すと毎回送り直して詰まる。
                if (auto* rejected = r.getProperty ("rejected", {}).getArray())
                    for (const auto& x : *rejected)
                        log ("sync: guide " + x.getProperty ("ara_id", {}).toString() + " -> " + x.getProperty ("guide", {}).toString()
                             + " rejected: " + x.getProperty ("reason", {}).toString());

                std::lock_guard guard (mutex);

                for (const auto& [id, guideId] : guidesReady)
                    if (const auto p = pendingGuides.find (id); p != pendingGuides.end() && p->second == guideId)
                        pendingGuides.erase (p);
            }
        }
    }

    // 5. 試験用の編集（GLISS_TEST_EDIT）
    applyTestEdit (m);

    if (options.testEdit.has_value() && ! testEditDone)
        workLeft = true;

    // 6. 版が変わった修飾の差分の再合成と、DAW に返すノート
    juce::StringArray contentChanged, caughtUp;          // 音が変わった修飾・戻した状態に追いついただけの修飾
    juce::StringArray stateDirty;                        // 保存の状態がホストの持っているものと違ってきた修飾
    std::map<juce::String, juce::String> currentStates;  // ara_revs の保存の状態の署名
    bool statesKnown = false;
    std::map<juce::String, juce::String> notesTargets;   // ノートを取り直す修飾 → ara_revs の版
    const auto revs = call ("ara_revs", object ({}), 60000);
    ExternalChanges::Result externalChange;

    if (isFailure (revs))
    {
        log ("sync: ara_revs failed: " + failureReason (revs));
        workLeft = true;
    }
    else
    {
        // 外部の AI（中継。engine/vocal_engine/ara_relay.py）が曲を変えた: 版の変わった修飾は下で取り直し、画面には後で知らせる
        externalChange = external.update (revs);
        statesKnown = revs.hasProperty ("states");   // 前の版のエンジンは保存の状態を返さない（知らせは音とノートだけ）

        if (revs.hasProperty ("guides"))
        {
            std::lock_guard guard (mutex);
            takeGuidesLocked (revs.getProperty ("guide", {}), revs.getProperty ("guides", {}));
        }

        for (const auto& mod : m.modifications)
        {
            if (threadShouldExit() || engineDied)
                return;

            const auto a = applied.find (mod.araId);

            if (a == applied.end() || ! a->second.registered)
                continue;

            const auto target = revs.getProperty ("revs", {}).getProperty (juce::Identifier (mod.araId), {}).toString();

            if (target.isEmpty())
                continue;

            openedEdits.emplace (mod.araId, editSignature (target));   // 返り値に版の無いエンジン: 登録の後に最初に見た編集

            if (const auto n = notesRev.find (mod.araId); n == notesRev.end() || n->second != target)
                notesTargets[mod.araId] = target;

            // 素材違いで当てていないアーカイブは、利用者がこの修飾を編集したら捨てる（エンジンの編集を保存する）。
            bool restorePending = false;
            {
                std::lock_guard guard (mutex);

                if (const auto p = pendingRestores.find (mod.araId); p != pendingRestores.end() && p->second.mismatch)
                {
                    if (p->second.editSignature.isEmpty())
                        p->second.editSignature = editSignature (target);
                    else if (p->second.editSignature != editSignature (target))
                        pendingRestores.erase (p);
                }

                restorePending = pendingRestores.count (mod.araId) > 0;
            }

            // 保存の状態（アーカイブに入るもの）がホストの持っているものと違ってきたか。戻し途中・素材違いで当てていない
            // アーカイブがあれば、保存に書くのはそのアーカイブ（変わっていない）
            if (const auto state = revs.getProperty ("states", {}).getProperty (juce::Identifier (mod.araId), {}).toString();
                state.isNotEmpty())
            {
                currentStates[mod.araId] = state;

                if (restorePending)
                    hostStates[mod.araId] = state;
                else if (const auto h = hostStates.find (mod.araId); h == hostStates.end())
                    hostStates.emplace (mod.araId, state);
                else if (h->second != state)
                    stateDirty.addIfNotAlreadyThere (mod.araId);
            }

            const auto current = localRev.find (mod.araId);

            if (current != localRev.end() && current->second == target)
            {
                if (getModStatus (mod.araId).state == "waiting")
                    workLeft = true;
                else
                    setModState (mod.araId, "ready");

                continue;
            }

            if (! renderModification (mod, stateDirty.contains (mod.araId), contentChanged, caughtUp))
                workLeft = true;

            changed.addIfNotAlreadyThere (mod.araId);
        }
    }

    if (callbacks.contentChanged)
    {
        if (! contentChanged.isEmpty())
            callbacks.contentChanged (contentChanged, true);

        for (const auto& id : contentChanged)
            caughtUp.removeString (id);

        if (! caughtUp.isEmpty())
            callbacks.contentChanged (caughtUp, false);
    }

    // 保存するものだけが変わった（音の変化は知らせていない）修飾と、ガイドの指定: ホストに知らせる（知らせないとホストが
    // 保存を求めず、ガイドの指定・歌詞・方式・取り消しが失われる）。音の変化を知らせた修飾は、ホストがそれで保存し直す
    if (statesKnown && ! isFailure (revs))
    {
        juce::StringArray stateOnly;

        for (const auto& id : stateDirty)
            if (! contentChanged.contains (id))
                stateOnly.add (id);

        for (const auto& id : contentChanged)
            if (const auto c = currentStates.find (id); c != currentStates.end())
                hostStates[id] = c->second;

        for (const auto& id : stateDirty)
            hostStates[id] = currentStates[id];

        // 戻し終えたか（ガイドの指定を書いていない古いアーカイブは、戻し終えた後の指定をホストの持っているものにする）
        bool ready = true;
        {
            std::lock_guard guard (mutex);
            ready = ! guidePending && pendingGuides.empty()
                    && std::none_of (pendingRestores.begin(), pendingRestores.end(), [] (const auto& p) { return ! p.second.mismatch; });
        }

        for (const auto& mod : m.modifications)
        {
            const auto* s = findSource (mod.sourceId);
            const auto a = applied.find (mod.araId);

            if (s != nullptr && s->samplesAvailable && (a == applied.end() || ! a->second.registered)
                && getModStatus (mod.araId).state != "failed")
                ready = false;
        }

        const auto guideMods = guidesChangedSinceHost (m, ready);

        for (const auto& id : guideMods)
            if (! contentChanged.contains (id))
                stateOnly.addIfNotAlreadyThere (id);

        if ((! stateOnly.isEmpty() || ! guideMods.isEmpty()) && callbacks.stateChanged)
        {
            log ("sync: saved state changed: " + stateOnly.joinIntoString (", ")
                 + (guideMods.isEmpty() ? juce::String() : " (guides: " + guideMods.joinIntoString (", ") + ")"));
            callbacks.stateChanged (stateOnly, ! guideMods.isEmpty());
        }

        for (const auto& id : stateOnly)
            changed.addIfNotAlreadyThere (id);   // 保存用の写しも取り直す
    }

    if (externalChange.projectChanged || externalChange.sessionChanged)
    {
        log ("sync: external edit (track " + externalChange.trackId + (externalChange.sessionChanged ? ", session" : "") + ")");

        // 画面は project-changed で描き直し、session-changed でトラックの一覧・ガイドを読み直す
        auto* o = new juce::DynamicObject();
        o->setProperty ("track_id", externalChange.trackId.isNotEmpty() ? juce::var (externalChange.trackId) : juce::var());
        o->setProperty ("external", true);
        emit ("project-changed", juce::var (o));

        if (externalChange.sessionChanged)
            sessionChanged = true;
    }

    if (! notesTargets.empty() && ! threadShouldExit() && ! engineDied)
        refreshNotes (m, notesTargets);

    // 7. 保存用の写し
    juce::Array<juce::var> ids;

    for (const auto& id : changed)
        ids.add (id);

    if (externalChange.sessionChanged)
        refreshArchivesLocked (object ({}), 60000);   // 外部がガイドを変えたかもしれない（保存に書くガイド）
    else if (! ids.isEmpty())
        refreshArchivesLocked (object ({ { "ara_ids", ids } }), 60000);

    for (const auto& mod : m.modifications)
        if (mod.pcm != nullptr)
            mod.pcm->releaseUnusedSnapshots();

    if (sessionChanged)
        emit ("session-changed", object ({ { "dir", getWorkDir().getFullPathName() } }));

    // ソースの読み出しが許されていない修飾・失敗したものは待たない（できることが無い）。
    for (const auto& mod : m.modifications)
    {
        const auto* s = findSource (mod.sourceId);

        if (s == nullptr || ! s->samplesAvailable)
            continue;

        const auto a = applied.find (mod.araId);

        if ((a == applied.end() || ! a->second.registered) && getModStatus (mod.araId).state != "failed")
            workLeft = true;
    }

    finish (workLeft);
}

} // namespace gliss
