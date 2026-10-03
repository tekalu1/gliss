#pragma once

// GlissHostCheck --editor: プラグインのエディタの画面（editor::EditorWebView）を、偽の DocumentBridge につないで
// このプロセスの中で開き、画面（app/renderer）↔ C++ の橋を確かめる。窓は画面の外に置く（前面にも入力の対象にもならない）。
//
//   1. 画面が読み込まれ ui-ready が来る（それより前の知らせは捨てられる）。window.api（ara-bridge.js）・data-mode・キーの転送の層がある
//   2. 画面が bootstrap を呼ぶ。user script から import('https://juce.backend/juce/index.js') が通る
//   3. engineCall が偽のブリッジに届き、値（{ok:false} も）が JS に返る。別スレッドからの completion も返る。ほかのネイティブ関数
//   4. /fs/: 作業場所の下（空白・%・+・日本語の名前）は読め、外・.. ・無いファイル・知らない資源は「読み取りを許していない場所」
//   5. ドキュメントの知らせ 6 種が画面の受け手に届く（JS で受けた記録を evaluateJavascript で読む）
//   6. 画面が使わないキー（F8）は窓へ渡り、画面が使うキー（Space）は渡らない
//   7. 応答の前にエディタを閉じても落ちない。開閉を N 回（既定 20）、2 つ同時に
#include "FakeDocumentBridge.h"

#include "editor/EditorWebView.h"

#include <juce_gui_extra/juce_gui_extra.h>

class EditorCheck final : private juce::Timer
{
public:
    using Report = std::function<void (const juce::String&)>;
    using Check = std::function<bool (bool, const juce::String&)>;

    EditorCheck (Report reportIn, Check checkIn, bool expectWebDirIn, int cyclesIn)
        : report (std::move (reportIn)), check (std::move (checkIn)), expectWebDir (expectWebDirIn), cycles (cyclesIn)
    {
    }

    ~EditorCheck() override
    {
        stopTimer();
        views.clear();
        windows.clear();
        fake.reset();
        root.deleteRecursively();
    }

    void start (std::function<void()> onFinishedIn)
    {
        onFinished = std::move (onFinishedIn);

        root = juce::File::getSpecialLocation (juce::File::tempDirectory)
                   .getChildFile ("gliss-editor-check-" + juce::String (juce::Time::getMillisecondCounter()));
        workDir = root.getChildFile ("work").getChildFile ("ara").getChildFile ("k1");
        workDir.createDirectory();

        jsonFile = workDir.getChildFile (juce::String::fromUTF8 ("view data %+日本語.json"));
        jsonFile.replaceWithText (juce::String::fromUTF8 ("{\"hello\":\"世界\",\"n\":1}"));

        binFile = workDir.getChildFile ("wave+%25.bin");
        juce::MemoryBlock bytes;
        for (int i = 0; i < 256; ++i)
            bytes.append (&i, 1);
        binFile.replaceWithData (bytes.getData(), bytes.getSize());

        outsideFile = root.getChildFile ("outside.json");
        outsideFile.replaceWithText ("{\"secret\":true}");

        fake = std::make_unique<FakeDocumentBridge> (workDir);

        openViews (1);
        fake->emit ("engine", state ("starting"));   // ui-ready の前: 捨てられる

        steps = {
            [this] { waitUntil ([this] { return views[0]->isUiReady(); }, 30000, "page (app/renderer) loaded and sent ui-ready"); },
            [this] { stepBasics(); },
            [this] { stepImport(); },
            [this] { stepEngineCall(); },
            [this] { stepOtherNatives(); },
            [this] { stepFs(); },
            [this] { stepEvents(); },
            [this] { stepKeys(); },
            [this] { stepLateCompletion(); },
            [this] { stepCycles(); },
            [this] { stepEnd(); },
        };
        next();
    }

private:
    //==============================================================================
    void next()
    {
        if (stepIndex < steps.size())
        {
            auto step = steps[stepIndex++];
            juce::MessageManager::callAsync (step);
        }
    }

    void fail (const juce::String& what)
    {
        check (false, what);
        abortRun();
    }

    void abortRun()
    {
        stopTimer();
        stepIndex = steps.size() - 1;   // 後片付け（stepEnd）だけを流す
        next();
    }

    /** pred が true になるまで待つ（50 ms ごと）。 */
    void waitUntil (std::function<bool()> pred, int timeoutMs, const juce::String& what, bool required = true)
    {
        waiting = { std::move (pred), timeoutMs, what, juce::Time::getMillisecondCounter(), required };
        startTimer (50);
    }

    void timerCallback() override
    {
        const auto elapsed = (int) (juce::Time::getMillisecondCounter() - waiting.start);

        if (waiting.pred())
        {
            stopTimer();
            check (true, waiting.what + " (" + juce::String (elapsed) + " ms)");
            return next();
        }

        if (elapsed > waiting.timeoutMs)
        {
            stopTimer();
            if (waiting.required)
                return fail (waiting.what + " within " + juce::String (waiting.timeoutMs) + " ms");

            check (false, waiting.what + " within " + juce::String (waiting.timeoutMs) + " ms");
            next();
        }
    }

    /** expression を評価し、null・undefined でない値が返るまで繰り返す。返った値を then に渡す。 */
    void jsPoll (const juce::String& expression, int timeoutMs, const juce::String& what, std::function<void (const juce::var&)> then)
    {
        const auto started = juce::Time::getMillisecondCounter();
        pollOnce (expression, timeoutMs, what, std::move (then), started);
    }

    void pollOnce (const juce::String& expression, int timeoutMs, const juce::String& what,
                   std::function<void (const juce::var&)> then, juce::uint32 started)
    {
        if (views.empty())
            return fail (what + ": no editor");

        views[0]->evaluateJavascript ("JSON.stringify((() => { try { return (" + expression + ") ?? null; } catch (e) { return null; } })())",
                                      [this, expression, timeoutMs, what, then, started] (juce::WebBrowserComponent::EvaluationResult r)
        {
            juce::var value;

            if (const auto* result = r.getResult())
                value = juce::JSON::fromString (result->toString());   // JSON::parse は最上位の true・文字列を読まない

            if (! value.isVoid() && ! value.isUndefined())
                return then (value);

            if ((int) (juce::Time::getMillisecondCounter() - started) > timeoutMs)
                return fail (what + ": no value within " + juce::String (timeoutMs) + " ms");

            juce::Timer::callAfterDelay (50, [this, expression, timeoutMs, what, then, started]
            {
                pollOnce (expression, timeoutMs, what, then, started);
            });
        });
    }

    void js (const juce::String& script)
    {
        views[0]->evaluateJavascript (script, [this] (juce::WebBrowserComponent::EvaluationResult r)
        {
            if (const auto* error = r.getError())
                report ("info: script error: " + error->message);
        });
    }

    static juce::String quoted (const juce::String& s) { return juce::JSON::toString (juce::var (s)); }

    static juce::var state (const juce::String& s)
    {
        auto* o = new juce::DynamicObject();
        o->setProperty ("state", s);
        return juce::var (o);
    }

    //==============================================================================
    void stepBasics()
    {
        check (views[0]->getNumEventsDropped() >= 1, "events before ui-ready are dropped (" + juce::String (views[0]->getNumEventsDropped()) + ")");

        if (expectWebDir)
            check (views[0]->isServingFromFolder(), "serving the page from GLISS_PLUGIN_WEB_DIR");

        jsPoll ("window.api && window.__app ? { mode: window.api.mode, dataMode: document.documentElement.dataset.mode, "
                "keys: !!window.__glissKeyForward, marker: !!document.querySelector('meta[name=\"gliss-test-marker\"]') } : null",
                15000, "window.api and the page modules", [this] (const juce::var& v)
        {
            check (v["mode"] == "ara", "window.api.mode = ara (ara-bridge.js as the user script)");
            check (v["dataMode"] == "ara", "html[data-mode=ara]");
            check ((bool) v["keys"], "key forwarding script is loaded");
            check ((bool) v["marker"] == expectWebDir, juce::String ("page is ") + (expectWebDir ? "the folder's copy (marker found)" : "the embedded copy (no marker)"));
            waitUntil ([this] { return fake->bootstrapCount >= 1; }, 5000, "the page called bootstrap");
        });
    }

    void stepImport()
    {
        // ara-bridge.js（user script）は最初のネイティブ関数の呼び出しで import('https://juce.backend/juce/index.js') をする。
        // bootstrap が C++ に届いた（上）ことと、その資源が読み込まれたことで、user script からの動的 import が通ったと分かる
        js ("window.__t = {};");
        jsPoll ("performance.getEntriesByType('resource').map((e) => e.name).filter((n) => n.endsWith('/juce/index.js'))",
                10000, "resource entries", [this] (const juce::var& v)
        {
            check (v.isArray() && v.size() >= 1 && fake->bootstrapCount >= 1,
                   "the user script imported /juce/index.js and called a native function (" + juce::JSON::toString (v, true) + ")");
            next();
        });
    }

    void stepEngineCall()
    {
        js ("window.api.call('engine_info', { probe: " + quoted (juce::String::fromUTF8 ("あ %+\\")) + " }).then((r) => { window.__t.call = r; });"
            "window.api.call('fail_tool', {}).then((r) => { window.__t.fail = r; });"
            "window.api.call('slow_tool', { delay_ms: 150 }).then((r) => { window.__t.slow = r; });");

        jsPoll ("window.__t.call && window.__t.fail && window.__t.slow ? window.__t : null", 10000, "engineCall results", [this] (const juce::var& t)
        {
            const auto call = t["call"];
            check ((bool) call["ok"] && call["version"] == "fake-engine", "engineCall('engine_info') returns the bridge's value to JS");
            check (call["echo"]["probe"] == juce::String::fromUTF8 ("あ %+\\"), "engineCall args reach C++ unchanged (" + call["echo"]["probe"].toString() + ")");
            check (fake->lastArgs["engine_info"]["probe"] == juce::String::fromUTF8 ("あ %+\\"), "the fake bridge saw the args");
            check (t["fail"]["ok"].isBool() && ! (bool) t["fail"]["ok"] && t["fail"]["error"] == juce::String::fromUTF8 ("偽の失敗"),
                   "{ok:false} comes back as a value, not an exception");
            check ((bool) t["slow"]["ok"], "a completion from another thread reaches JS");
            next();
        });
    }

    void stepOtherNatives()
    {
        js ("Promise.all([window.api.transport('toggle'), window.api.preview('stop'), window.api.setCompare(true), window.api.hostState(),"
            " window.api.saveState({ zoom: 2 }), window.api.restartEngine(), window.api.reveal('C:\\\\gliss-no-such-dir\\\\x.json')])"
            ".then((v) => { window.__t.misc = v; }, (e) => { window.__t.misc = 'ERR ' + e; })");

        jsPoll ("window.__t.misc", 10000, "other native functions", [this] (const juce::var& v)
        {
            if (! check (v.isArray() && v.size() == 7, "native functions answered: " + juce::JSON::toString (v, true)))
                return next();

            check ((bool) v[0]["ok"] && fake->transportOps.contains ("toggle"), "transport('toggle') reaches the bridge");
            check ((bool) v[1]["ok"] && fake->previewOps.contains ("stop"), "preview('stop') reaches the bridge");
            check ((bool) v[2] && fake->compare, "setCompare(true) reaches the bridge");
            check (v[3]["engine"]["state"] == "ready" && fake->hostStateCount >= 1, "hostState() returns the bridge's state");
            check ((bool) v[4] && fake->savedPatches.size() >= 1 && (int) fake->savedPatches.getLast()["zoom"] == 2, "saveState(patch) reaches the bridge");
            check ((bool) v[5]["ok"] && fake->restartCount == 1, "restartEngine() completes");
            check (v[6].isBool() && ! (bool) v[6], "reveal() refuses a path outside the work folder");
            next();
        });
    }

    void stepFs()
    {
        const auto traversal = workDir.getFullPathName() + "\\..\\..\\..\\outside.json";
        const auto missing = workDir.getChildFile ("missing.json").getFullPathName();

        js ("(async () => { const r = {}; const api = window.api;"
            " try { r.json = await api.readJson(" + quoted (jsonFile.getFullPathName()) + "); } catch (e) { r.json = 'ERR ' + e.message; }"
            " try { const b = new Uint8Array(await api.readFile(" + quoted (binFile.getFullPathName()) + "));"
            "       r.bin = [b.length, b.reduce((a, x) => a + x, 0), b[255]]; } catch (e) { r.bin = 'ERR ' + e.message; }"
            " for (const [k, p] of [['outside', " + quoted (outsideFile.getFullPathName()) + "], ['traversal', " + quoted (traversal) + "],"
            "                       ['missing', " + quoted (missing) + "], ['folder', " + quoted (workDir.getFullPathName()) + "]]) {"
            "   try { await api.readJson(p); r[k] = 'READ'; } catch (e) { r[k] = e.message; } }"
            " const res = await fetch('/nope.js'); r.unknown = res.headers.get('content-type');"
            " window.__t.fs = r; })()");

        jsPoll ("window.__t.fs", 10000, "/fs/ reads", [this] (const juce::var& r)
        {
            const auto denied = juce::String::fromUTF8 ("読み取りを許していない場所");
            check (r["json"]["hello"] == juce::String::fromUTF8 ("世界"), "readJson of a name with space, %, + and Japanese (" + juce::JSON::toString (r["json"], true) + ")");
            check (r["bin"].isArray() && (int) r["bin"][0] == 256 && (int) r["bin"][1] == 32640 && (int) r["bin"][2] == 255,
                   "readFile returns the bytes (" + juce::JSON::toString (r["bin"], true) + ")");

            for (auto* key : { "outside", "traversal", "missing", "folder" })
                check (r[key].toString().startsWith (denied), juce::String ("/fs/ refuses ") + key + " (" + r[key].toString() + ")");

            check (r["unknown"].toString().startsWith ("application/x-gliss-not-found"), "an unknown resource is not found (" + r["unknown"].toString() + ")");
            next();
        });
    }

    void stepEvents()
    {
        js ("(() => { window.__t.ev = []; const rec = (n) => (p) => window.__t.ev.push([n, p]); const a = window.api;"
            " a.onPlayhead(rec('playhead')); a.onSelection(rec('selection')); a.onSessionChanged(rec('session-changed'));"
            " a.onProjectChanged(rec('project-changed')); a.onCacheState(rec('cache')); a.onEngineState(rec('engine'));"
            " window.__t.recReady = true; })();");

        jsPoll ("window.__t.recReady", 5000, "event recorders", [this] (const juce::var&)
        {
            sentBefore = views[0]->getNumEventsSent();

            auto obj = [] (std::initializer_list<std::pair<const char*, juce::var>> props)
            {
                auto* o = new juce::DynamicObject();
                for (auto& [k, v] : props)
                    o->setProperty (k, v);
                return juce::var (o);
            };

            juce::Array<juce::var> loop { 1.0, 2.5 };
            fake->emit ("playhead", obj ({ { "song_sec", 1.25 }, { "playing", false }, { "loop", loop }, { "mapped", obj ({ { "t-test", 0.5 } }) } }));
            fake->emit ("selection", obj ({ { "track_id", "t-test" }, { "ara_id", "a1" },
                                             { "region", obj ({ { "id", "r1" }, { "song_start", 0.0 }, { "song_end", 2.0 }, { "mod_start", 0.0 }, { "mod_end", 2.0 } }) } }));
            fake->emit ("session-changed", obj ({ { "dir", "" } }));
            fake->emit ("project-changed", obj ({ { "track_id", "t-test" } }));
            fake->emit ("cache", obj ({ { "track_id", "t-test" }, { "state", "failed" }, { "error", juce::String::fromUTF8 ("テストの失敗") } }));
            fake->emit ("engine", state ("ready"));

            jsPoll ("window.__t.ev.length >= 6 ? window.__t.ev : null", 10000, "events in JS", [this] (const juce::var& ev)
            {
                juce::StringArray names;
                for (const auto& e : *ev.getArray())
                    names.add (e[0].toString());

                check (names.joinIntoString (",") == "playhead,selection,session-changed,project-changed,cache,engine",
                       "all 6 document events reach the page in order (" + names.joinIntoString (",") + ")");
                check ((double) ev[0][1]["song_sec"] == 1.25 && (double) ev[0][1]["loop"][1] == 2.5, "playhead payload intact");
                check (ev[1][1]["region"]["id"] == "r1", "selection payload intact");
                check (ev[4][1]["error"] == juce::String::fromUTF8 ("テストの失敗"), "cache payload intact (Japanese)");
                check (views[0]->getNumEventsSent() - sentBefore == 6, "the editor sent 6 events after ui-ready");
                next();
            });
        });
    }

    void stepKeys()
    {
        const auto dispatch = [] (const juce::String& type, const juce::String& key, const juce::String& code, int keyCode)
        {
            return "(() => { const e = new KeyboardEvent('" + type + "', { key: " + quoted (key) + ", code: " + quoted (code)
                 + ", bubbles: true, cancelable: true }); Object.defineProperty(e, 'keyCode', { get: () => " + juce::String (keyCode)
                 + " }); (document.activeElement || document.body).dispatchEvent(e); })();";
        };

        keysBefore = views[0]->getNumKeysForwarded();
        transportsBefore = fake->transportOps.size();
        recorder.keys.clear();

        js (dispatch ("keydown", "F8", "F8", 119) + dispatch ("keyup", "F8", "F8", 119));

        waitUntil ([this] { return recorder.keys.contains (juce::KeyPress (juce::KeyPress::F8Key)); }, 3000,
                   "a key the page does not use (F8) reaches the plug-in window", false);

        steps.insert (steps.begin() + (std::ptrdiff_t) stepIndex, [this, dispatch]
        {
            check (views[0]->getNumKeysForwarded() - keysBefore == 2, "F8 down and up were forwarded (" + juce::String (views[0]->getNumKeysForwarded() - keysBefore) + ")");
            keysBefore = views[0]->getNumKeysForwarded();
            recorder.keys.clear();
            js (dispatch ("keydown", " ", "Space", 32) + dispatch ("keyup", " ", "Space", 32));

            juce::Timer::callAfterDelay (700, [this]
            {
                check (views[0]->getNumKeysForwarded() == keysBefore && recorder.keys.isEmpty(), "Space (the page's play/stop) is not forwarded");
                report ("info: Space -> transport(toggle) calls: " + juce::String (fake->transportOps.size() - transportsBefore)
                        + " (the page handles Space as its play command)");
                next();
            });
        });
    }

    void stepLateCompletion()
    {
        const auto before = fake->calls.size();
        js ("window.api.call('slow_tool', { delay_ms: 700 });");

        waitUntil ([this, before] { return fake->calls.size() > before; }, 5000, "slow call reached the bridge");

        steps.insert (steps.begin() + (std::ptrdiff_t) stepIndex, [this]
        {
            closeViews();   // 応答の前に閉じる
            juce::Timer::callAfterDelay (1500, [this]
            {
                check (fake->listeners.isEmpty(), "the editor removed its listener when closed");
                check (true, "a completion after the editor closed was dropped without a crash");
                next();
            });
        });
    }

    void stepCycles()
    {
        if (cycles <= 0)
            return next();

        cycleIndex = 0;
        runCycle();
    }

    void runCycle()
    {
        if (cycleIndex >= cycles)
        {
            check (true, "opened and closed 2 editors " + juce::String (cycles) + " times (slowest ready " + juce::String (slowestReadyMs) + " ms)");
            report ("info: ms until both were ready, per cycle: " + readyTimes.joinIntoString (" "));
            return next();
        }

        ++cycleIndex;
        openViews (2);
        const auto started = juce::Time::getMillisecondCounter();

        waitForBoth (started);
    }

    void waitForBoth (juce::uint32 started)
    {
        const auto elapsed = (int) (juce::Time::getMillisecondCounter() - started);

        if (views.size() == 2 && views[0]->isUiReady() && views[1]->isUiReady())
        {
            slowestReadyMs = juce::jmax (slowestReadyMs, elapsed);
            readyTimes.add (juce::String (elapsed));
            closeViews();
            juce::Timer::callAfterDelay (100, [this] { runCycle(); });
            return;
        }

        if (elapsed > 30000)
            return fail ("cycle " + juce::String (cycleIndex) + ": both editors ready within 30 s");

        juce::Timer::callAfterDelay (50, [this, started] { waitForBoth (started); });
    }

    void stepEnd()
    {
        closeViews();
        check (fake == nullptr || fake->listeners.isEmpty(), "no editor is left listening");

        if (onFinished != nullptr)
            std::exchange (onFinished, nullptr)();
    }

    //==============================================================================
    void openViews (int count)
    {
        closeViews();

        for (int i = 0; i < count; ++i)
        {
            auto view = std::make_unique<gliss::editor::EditorWebView> (*fake);
            view->setSize (1100, 680);
            auto window = std::make_unique<OffscreenHost> (*view, i);
            window->addKeyListener (&recorder);
            views.push_back (std::move (view));
            windows.push_back (std::move (window));
        }
    }

    void closeViews()
    {
        // DAW と同じく、窓より先にエディタを壊す
        for (auto& w : windows)
            w->removeKeyListener (&recorder);

        views.clear();
        windows.clear();
    }

    /** 画面の外の窓（GlissHostCheck の OffscreenWindow と同じ置き方）。 */
    class OffscreenHost final : public juce::Component
    {
    public:
        OffscreenHost (juce::Component& content, int index)
        {
            addAndMakeVisible (content);
            setBounds (-20000 - index * 1200, -20000, content.getWidth(), content.getHeight());
            setWantsKeyboardFocus (false);
            addToDesktop (juce::ComponentPeer::windowIsTemporary);
            setVisible (true);
        }

        void resized() override
        {
            for (auto* c : getChildren())
                c->setBounds (getLocalBounds());
        }
    };

    struct KeyRecorder final : public juce::KeyListener
    {
        bool keyPressed (const juce::KeyPress& key, juce::Component*) override
        {
            keys.add (key);
            return true;
        }

        juce::Array<juce::KeyPress> keys;
    };

    struct Waiting
    {
        std::function<bool()> pred;
        int timeoutMs = 0;
        juce::String what;
        juce::uint32 start = 0;
        bool required = true;
    };

    Report report;
    Check check;
    bool expectWebDir = false;
    int cycles = 20;

    std::function<void()> onFinished;
    std::vector<std::function<void()>> steps;
    size_t stepIndex = 0;
    Waiting waiting;

    juce::File root, workDir, jsonFile, binFile, outsideFile;
    std::unique_ptr<FakeDocumentBridge> fake;
    std::vector<std::unique_ptr<gliss::editor::EditorWebView>> views;
    std::vector<std::unique_ptr<OffscreenHost>> windows;
    KeyRecorder recorder;

    int sentBefore = 0, keysBefore = 0, transportsBefore = 0;
    int cycleIndex = 0, slowestReadyMs = 0;
    juce::StringArray readyTimes;
};
