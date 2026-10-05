#pragma once

// GlissHostCheck の --editor で使う、偽の DocumentBridge（C3 の GlissDocumentController の代わり）。
// エンジンを持たず、決まった値で答え、呼ばれたものを記録する。
#include "AraSelectionShim.h"

#include <juce_gui_basics/juce_gui_basics.h>

#include "ara/DocumentBridge.h"

#include <thread>

class FakeDocumentBridge final : public gliss::DocumentBridge
{
public:
    explicit FakeDocumentBridge (juce::File workDirIn) : workDir (std::move (workDirIn)) {}

    ~FakeDocumentBridge() override
    {
        for (auto& t : workers)
            if (t.joinable())
                t.join();
    }

    void engineCall (const juce::String& tool, const juce::var& args, Completion done) override
    {
        calls.add (tool);
        lastArgs.set (tool, args);

        auto* result = new juce::DynamicObject();

        if (tool == "engine_info")
        {
            result->setProperty ("ok", true);
            result->setProperty ("version", "fake-engine");
            result->setProperty ("echo", args);
        }
        else if (tool == "list_tracks")
        {
            result->setProperty ("ok", false);
            result->setProperty ("error", juce::String::fromUTF8 ("トラックが無い"));
        }
        else if (tool == "fail_tool")
        {
            result->setProperty ("ok", false);
            result->setProperty ("error", juce::String::fromUTF8 ("偽の失敗"));
        }
        else if (tool == "slow_tool")
        {
            // エンジンの応答を読むスレッドから返すのと同じ形（約束はメッセージスレッドだが、外れても落ちないことを見る）
            result->setProperty ("ok", true);
            result->setProperty ("slow", true);
            const auto delayMs = (int) args.getProperty ("delay_ms", 200);
            workers.emplace_back ([done, delayMs, value = juce::var (result)]
            {
                juce::Thread::sleep (delayMs);
                done (value);
            });
            return;
        }
        else
        {
            result->setProperty ("ok", false);
            result->setProperty ("error", "fake engine has no tool " + tool);
        }

        juce::MessageManager::callAsync ([done, value = juce::var (result)] { done (value); });
    }

    juce::var bootstrap() override
    {
        ++bootstrapCount;
        auto* b = new juce::DynamicObject();
        b->setProperty ("engineReady", true);
        b->setProperty ("version", "fake");
        b->setProperty ("hostCanTransport", true);
        b->setProperty ("compare", false);
        b->setProperty ("selection", juce::var());
        return juce::var (b);
    }

    void saveState (const juce::var& patch) override { savedPatches.add (patch); }

    juce::var transport (const juce::String& op, const juce::var&) override
    {
        transportOps.add (op);
        return ok();
    }

    void preview (const juce::String& op, const juce::var&, Completion done) override
    {
        previewOps.add (op);
        done (ok());
    }

    void setCompare (bool on) override { compare = on; }

    juce::var hostState() override
    {
        ++hostStateCount;
        auto* engine = new juce::DynamicObject();
        engine->setProperty ("state", "ready");
        engine->setProperty ("error", "");

        auto* h = new juce::DynamicObject();
        h->setProperty ("selection", juce::var());
        h->setProperty ("playhead", juce::var());
        h->setProperty ("tracks", juce::Array<juce::var>());
        h->setProperty ("engine", juce::var (engine));
        return juce::var (h);
    }

    void restartEngine (Completion done) override
    {
        ++restartCount;
        juce::MessageManager::callAsync ([done] { done (ok()); });
    }

    bool isReadableByEditor (const juce::File& file) override
    {
        ++readChecks;
        return file.isAChildOf (workDir);
    }

    void addListener (Listener* l) override { listeners.addIfNotAlreadyThere (l); }
    void removeListener (Listener* l) override { listeners.removeFirstMatchingValue (l); }

    void editorSelectionChanged (const juce::ARAViewSelection&) override { ++selectionChanges; }

    /** ドキュメントの知らせを、登録しているエディタ全部へ送る。 */
    void emit (const juce::String& name, const juce::var& data)
    {
        for (auto* l : juce::Array<Listener*> (listeners))
            l->documentEvent (name, data);
    }

    static juce::var ok()
    {
        auto* o = new juce::DynamicObject();
        o->setProperty ("ok", true);
        return juce::var (o);
    }

    juce::File workDir;
    juce::StringArray calls, transportOps, previewOps;
    juce::NamedValueSet lastArgs;
    juce::Array<juce::var> savedPatches;
    juce::Array<Listener*> listeners;
    bool compare = false;
    int bootstrapCount = 0, hostStateCount = 0, restartCount = 0, readChecks = 0, selectionChanges = 0;

private:
    std::vector<std::thread> workers;
};
