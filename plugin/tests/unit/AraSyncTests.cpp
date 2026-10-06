// DocumentSync がホストに「中身が変わった」「保存するものが変わった」を知らせるかどうかの単体テスト。
// エンジンの代わりに、決めた答えを返す偽のエンジン（python の小さな MCP サーバー）を使い、ara_revs・ara_render_dirty・
// ara_restore の答えを試験の途中で差し替えて、Callbacks::contentChanged（notifyHost）・stateChanged の呼ばれ方を見る。
// 本物のエンジンとホストで同じことを見るのは plugin/tests/aratest の GlissARATest -changes。
#include "ara/DocumentSync.h"

#include <juce_core/juce_core.h>

#include <cstdio>
#include <functional>
#include <mutex>
#include <vector>

namespace gliss
{

namespace
{
juce::File pythonForTests()
{
    const auto configured = juce::SystemStats::getEnvironmentVariable ("GLISS_ENGINE_PYTHON", {});
    if (configured.isNotEmpty())
        return juce::File (configured);
    wchar_t path[32768] {};
    if (::SearchPathW (nullptr, L"python.exe", nullptr, (DWORD) std::size (path), path, nullptr) != 0)
        return juce::File (juce::String (path));
    return {};
}

// 偽のエンジン: 呼ばれるたびに state.json を読み、修飾ごとに {rev, state, windows, pending} を返す。
// ara_render_dirty は since が前に返した版と違えば reset、windows なら 100 フレームの窓（中身は 0.5）。
const char* fakeEngine = R"PY(import json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
pcm = os.path.join(d, 'win.f32')
with open(pcm, 'wb') as f:
    f.write(b'\x00\x00\x00\x3f' * 100)
last = {}
def state():
    with open(os.path.join(d, 'state.json'), encoding='utf-8') as f:
        return json.load(f)
def reply(id, value):
    print(json.dumps({'jsonrpc': '2.0', 'id': id, 'result': {'structuredContent': {'result': value}}}), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if 'id' not in msg:
        continue
    if msg['method'] == 'initialize':
        print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'fake', 'version': '1'}}}), flush=True)
        continue
    name = msg['params']['name']
    args = msg['params'].get('arguments') or {}
    st = state()
    mods = st['mods']
    if name == 'engine_info':
        reply(msg['id'], {'ok': True, 'version': 'fake'})
    elif name == 'ara_open':
        reply(msg['id'], {'ok': True, 'dir': d})
    elif name == 'ara_set_modification':
        m = mods[args['ara_id']]
        reply(msg['id'], {'ok': True, 'track': {'id': 't-' + args['ara_id']}, 'rev': m['rev'], 'state': m['state']})
    elif name == 'ara_restore':
        m = mods[args['ara_id']]
        reply(msg['id'], {'ok': True, 'mismatch': False, 'edits': 1, 'rev': m['rev'], 'state': m['state'],
                          'render_changed': bool(st.get('render_changed'))})
    elif name == 'ara_revs':
        reply(msg['id'], {'ok': True, 'revs': {k: v['rev'] for k, v in mods.items()},
                          'states': {k: v['state'] for k, v in mods.items()},
                          'track_ids': {k: 't-' + k for k in mods}, 'guides': st.get('guides', {}), 'guide': st.get('guide')})
    elif name == 'ara_render_dirty':
        aid = args['ara_id']
        m = mods[aid]
        reset = args.get('since') is None or args.get('since') != last.get(aid)
        last[aid] = m['rev']
        win = [{'start_frame': 0, 'frames': 100, 'byte_offset': 0}] if m.get('windows') and not m.get('pending') else []
        reply(msg['id'], {'ok': True, 'rev': m['rev'], 'reset': reset, 'more': False, 'restore': [], 'windows': win,
                          'path': pcm if win else None, 'sr': 44100, 'channels': 1, 'source_frames': 1000,
                          'analysis_pending': bool(m.get('pending'))})
    elif name == 'ara_notes':
        reply(msg['id'], {'ok': True, 'notes': {}})
    elif name == 'ara_archive':
        reply(msg['id'], {'ok': True, 'archives': {}, 'guides': st.get('guides', {}), 'guide': st.get('guide')})
    elif name == 'ara_sync':
        reply(msg['id'], {'ok': True, 'rejected': []})
    else:
        reply(msg['id'], {'ok': True})
)PY";

struct Silence final : SourceSamples
{
    bool read (float* const* dest, int numChannels, juce::int64, int numSamples) override
    {
        for (int c = 0; c < numChannels; ++c)
            juce::FloatVectorOperations::clear (dest[c], numSamples);
        return true;
    }
};

/** 同期のスレッドから届く知らせを数える。 */
struct Recorder
{
    std::mutex m;
    std::vector<std::pair<juce::String, bool>> content;   // (ara_id, notifyHost)
    std::vector<std::pair<juce::String, bool>> state;     // (ara_id, documentData)。ara_id が空なら文書だけ

    int count (const std::vector<std::pair<juce::String, bool>>& v, const juce::String& id, bool flag)
    {
        std::lock_guard g (m);
        int n = 0;
        for (const auto& [x, f] : v)
            n += (x == id && f == flag) ? 1 : 0;
        return n;
    }

    void clear()
    {
        std::lock_guard g (m);
        content.clear();
        state.clear();
    }

    DocumentSync::Callbacks callbacks()
    {
        DocumentSync::Callbacks c;
        c.contentChanged = [this] (const juce::StringArray& ids, bool notifyHost)
        {
            std::lock_guard g (m);
            for (const auto& id : ids)
                content.emplace_back (id, notifyHost);
        };
        c.stateChanged = [this] (const juce::StringArray& ids, bool documentData)
        {
            std::lock_guard g (m);
            for (const auto& id : ids)
                state.emplace_back (id, documentData);
            if (ids.isEmpty())
                state.emplace_back (juce::String(), documentData);
        };
        c.log = [] (const juce::String& line) { std::printf ("  %s\n", line.toRawUTF8()); std::fflush (stdout); };
        return c;
    }
};

bool waitUntil (const std::function<bool()>& done, int timeoutMs)
{
    for (int waited = 0; waited < timeoutMs; waited += 20)
    {
        if (done())
            return true;
        juce::Thread::sleep (20);
    }
    return done();
}

SyncModel twoModifications()
{
    SyncModel model;
    model.workKey = "fake-key";
    SyncSource s;
    s.id = "src";
    s.sampleRate = 44100.0;
    s.numChannels = 1;
    s.numSamples = 1000;
    s.generation = 1;
    s.samplesAvailable = true;
    s.samples = std::make_shared<Silence>();
    model.sources.push_back (s);

    for (const auto* id : { "mod", "mod2" })
    {
        SyncModification mod;
        mod.araId = id;
        mod.sourceId = "src";
        mod.pcm = std::make_shared<EditedPcm> (44100.0, 1);
        model.modifications.push_back (mod);
    }

    return model;
}
} // namespace

class AraSyncNotificationTests final : public juce::UnitTest
{
public:
    AraSyncNotificationTests() : juce::UnitTest ("ARA host notifications with a scripted engine", "Gliss") {}

    void runTest() override
    {
        const auto python = pythonForTests();
        beginTest ("Python is available for the scripted engine");
        expect (python.existsAsFile());
        if (! python.existsAsFile())
            return;

        dir = juce::File::getSpecialLocation (juce::File::tempDirectory).getNonexistentChildFile ("GlissSyncTest", {}, true);
        expect (dir.createDirectory());
        expect (dir.getChildFile ("fake_engine.py").replaceWithText (fakeEngine));
        config.executable = python;
        config.arguments.add (dir.getChildFile ("fake_engine.py").getFullPathName());
        config.workingDirectory = dir;
        config.environment.set ("PYTHONIOENCODING", "utf-8");

        DocumentSync::Options options;
        options.pollMs = 50;

        beginTest ("catching up with the opened state is not reported; a state-only change is");
        {
            write ("a0:e0", "s0", "a0:f0", "t0", {});
            Recorder rec;
            DocumentSync sync (config, options, rec.callbacks());
            sync.setModel (twoModifications());
            expect (waitUntil ([&] { return rec.count (rec.content, "mod", false) > 0; }, 15000), "first render caught up");
            settle (sync);
            expectEquals (rec.count (rec.content, "mod", true), 0);
            expect (rec.state.empty());

            // 歌詞だけ・編集の無い修飾の方式だけ（音も編集の署名も同じで、保存の状態だけが違う）
            rec.clear();
            write ("a0:e0", "s1", "a0:f0", "t0", {});
            sync.requestSync();
            expect (waitUntil ([&] { return rec.count (rec.state, "mod", false) > 0; }, 10000), "state change reported");
            settle (sync);
            expectEquals (rec.count (rec.state, "mod", false), 1);
            expectEquals (rec.count (rec.state, "mod2", false), 0);
            expectEquals (rec.count (rec.content, "mod", true), 0);

            // 同じ状態のまま: もう知らせない
            rec.clear();
            sync.requestSync();
            settle (sync);
            expect (rec.state.empty() && rec.content.empty());

            // ガイドの指定（文書の保存の状態）
            rec.clear();
            write ("a0:e0", "s1", "a0:f0", "t0", { { "mod", "mod2" } });
            sync.requestSync();
            expect (waitUntil ([&] { return rec.count (rec.state, "mod", true) > 0; }, 10000), "guide change reported as document data");
            settle (sync);
            expectEquals (sync.getGuidesForStore().at ("mod"), juce::String ("mod2"));

            // 編集（音が変わる）: 音の知らせだけ（保存の状態の知らせは重ねない）
            rec.clear();
            write ("a0:e1", "s2", "a0:f0", "t0", { { "mod", "mod2" } });
            sync.requestSync();
            expect (waitUntil ([&] { return rec.count (rec.content, "mod", true) > 0; }, 10000), "edit reported");
            settle (sync);
            expectEquals (rec.count (rec.state, "mod", false), 0);

            // 解析待ちにしてから、編集を開いた時のもの（e0）へ取り消す: 開いた時の署名ではなく、最後に知らせた署名（e1）と
            // 比べるので知らせる（保存の状態はわざと同じにして、編集の署名の比べ方だけを見る）
            write ("a1:e1", "s2", "a0:f0", "t0", { { "mod", "mod2" } }, true);
            sync.requestSync();
            expect (waitUntil ([&] { return sync.getModStatus ("mod").state == "waiting"; }, 10000), "waiting for the analysis");
            rec.clear();
            write ("a2:e0", "s2", "a0:f0", "t0", { { "mod", "mod2" } });
            sync.requestSync();
            expect (waitUntil ([&] { return ! rec.content.empty(); }, 10000), "undo rendered");
            settle (sync);
            expectEquals (rec.count (rec.content, "mod", true), 1);
            expectEquals (rec.count (rec.content, "mod", false), 0);
            sync.shutdown();
        }

        beginTest ("restoring an archive is not reported unless the engine cannot reproduce its sound (render_changed)");
        for (const bool older : { false, true })
        {
            write ("a0:e0", "s0", "a0:f0", "t0", { { "mod", "mod2" } }, false, older);
            Recorder rec;
            DocumentSync sync (config, options, rec.callbacks());
            sync.setPendingRestore ("mod", parseArchive());
            sync.setPendingGuides ({ { "mod", "mod2" } });
            sync.setHostGuides ({ { "mod", "mod2" } }, {}, true);
            sync.setModel (twoModifications());
            expect (waitUntil ([&] { return ! rec.content.empty(); }, 15000), "first render");
            settle (sync);
            expectEquals (rec.count (rec.content, "mod", true), older ? 1 : 0);
            expectEquals (rec.count (rec.content, "mod", false), older ? 0 : 1);
            expect (rec.state.empty(), "no saved-state notification after a restore");
            sync.shutdown();
        }

        beginTest ("an old archive without guides takes the restored guides as the host's");
        {
            write ("a0:e0", "s0", "a0:f0", "t0", { { "mod", "mod2" } });
            Recorder rec;
            DocumentSync sync (config, options, rec.callbacks());
            sync.setPendingRestore ("mod", parseArchive());
            sync.setHostGuides ({}, {}, false);
            sync.setModel (twoModifications());
            expect (waitUntil ([&] { return ! rec.content.empty(); }, 15000), "first render");
            settle (sync);
            expect (rec.state.empty());
            rec.clear();
            write ("a0:e0", "s0", "a0:f0", "t0", {});
            sync.requestSync();
            expect (waitUntil ([&] { return rec.count (rec.state, "mod", true) > 0; }, 10000), "guide removal reported");
            sync.shutdown();
        }

        dir.deleteRecursively();
    }

private:
    juce::File dir;
    EngineConfig config;

    static juce::var parseArchive() { return juce::JSON::parse ("{\"changesets\": [{\"id\": \"c001\"}]}"); }

    void write (const char* rev, const char* state, const char* rev2, const char* state2,
                const std::map<juce::String, juce::String>& guides, bool pending = false, bool renderChanged = false)
    {
        auto* g = new juce::DynamicObject();
        for (const auto& [k, v] : guides)
            g->setProperty (juce::Identifier (k), v);

        const auto mod = [] (const char* r, const char* s, bool windows, bool p)
        {
            auto* o = new juce::DynamicObject();
            o->setProperty ("rev", r);
            o->setProperty ("state", s);
            o->setProperty ("windows", windows);
            o->setProperty ("pending", p);
            return juce::var (o);
        };

        auto* mods = new juce::DynamicObject();
        mods->setProperty ("mod", mod (rev, state, true, pending));
        mods->setProperty ("mod2", mod (rev2, state2, false, false));

        auto* root = new juce::DynamicObject();
        root->setProperty ("mods", juce::var (mods));
        root->setProperty ("guides", juce::var (g));
        root->setProperty ("render_changed", renderChanged);
        const auto file = dir.getChildFile ("state.json");
        const auto tmp = dir.getChildFile ("state.json.tmp");
        tmp.replaceWithText (juce::JSON::toString (juce::var (root)));
        for (int i = 0; i < 50 && ! tmp.moveFileTo (file); ++i)
            juce::Thread::sleep (10);
    }

    /** 同期が落ち着くまで（同期を 2 周させてから settled を待つ）。 */
    static void settle (DocumentSync& sync)
    {
        juce::Thread::sleep (200);
        waitUntil ([&] { return sync.isSettled(); }, 5000);
        juce::Thread::sleep (200);
    }
};

static AraSyncNotificationTests araSyncNotificationTests;

} // namespace gliss
