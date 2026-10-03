// 単位 C3（DocumentController への組み込み）のうち、エンジンも ARA のホストも要らない部分の単体テスト。
// エンジンにつないだ通し（編集 → 再生 → アーカイブの往復）は plugin/tests/aratest の GlissARATest と verify_ara_engine.py。
#include "ara/ArchiveIO.h"
#include "ara/DocumentSync.h"
#include "ara/EngineCalls.h"
#include "ara/FloatWavWriter.h"
#include "ara/PlayheadState.h"
#include "ara/PluginState.h"
#include "ara/RegionMapping.h"

#include <juce_audio_formats/juce_audio_formats.h>
#include <juce_core/juce_core.h>

#include <cmath>
#include <cstdio>

namespace gliss
{

namespace
{
RegionTimes region (const char* id, double songStart, double songEnd, double modStart, double modEnd)
{
    RegionTimes r;
    r.id = id;
    r.songStart = songStart;
    r.songEnd = songEnd;
    r.modStart = modStart;
    r.modEnd = modEnd;
    return r;
}

/** 段階 1 の PlaybackRenderer の計算（周波数が同じとき）。sliceBlock がこれと同じであること。 */
regions::BlockSlice stage1Slice (const RegionTimes& r, juce::int64 blockStart, int numSamples, double rate)
{
    const auto playbackRange = juce::Range<juce::int64> (regions::samplePosition (r.songStart, rate), regions::samplePosition (r.songEnd, rate));
    const auto blockRange = juce::Range<juce::int64>::withStartAndLength (blockStart, numSamples);
    auto renderRange = blockRange.getIntersectionWith (playbackRange);
    const juce::Range<juce::int64> modificationRange (regions::samplePosition (r.modStart, rate), regions::samplePosition (r.modEnd, rate));
    const auto modificationOffset = modificationRange.getStart() - playbackRange.getStart();
    renderRange = renderRange.getIntersectionWith (modificationRange.movedToStartAt (playbackRange.getStart()));

    regions::BlockSlice s;
    if (renderRange.isEmpty())
        return s;
    s.destStart = (int) (renderRange.getStart() - blockStart);
    s.numSamples = (int) renderRange.getLength();
    s.startInSource = renderRange.getStart() + modificationOffset;
    return s;
}

juce::var parseJson (const char* text)
{
    return juce::JSON::parse (juce::String::fromUTF8 (text));
}

/** テストの間だけ、JUCE のログ（失敗の文を含む）を標準出力に出す（この console のアプリは既定では出さない）。 */
struct ScopedStdoutLogger final : public juce::Logger
{
    ScopedStdoutLogger() : previous (juce::Logger::getCurrentLogger()) { juce::Logger::setCurrentLogger (this); }
    ~ScopedStdoutLogger() override { juce::Logger::setCurrentLogger (previous); }
    void logMessage (const juce::String& message) override { std::printf ("%s\n", message.toRawUTF8()); std::fflush (stdout); }
    juce::Logger* previous;
};
} // namespace

class AraArchiveTests final : public juce::UnitTest
{
public:
    AraArchiveTests() : juce::UnitTest ("ARA archive and keys", "Gliss") {}

    void runTest() override
    {
        const ScopedStdoutLogger logger;

        beginTest ("archive round trip keeps ids, names, edits and the work key");
        {
            DocumentArchive a;
            a.workKey = archive::makeWorkKey();
            a.guide = "mod \"B\"";
            a.modifications["mod A"] = { "Vocal 1", parseJson (R"({"format":"gliss-archive","changesets":[{"id":"c001"}],"lyrics":{"text":"あ"}})") };
            a.modifications["mod \"B\""] = { "ガイド\\1", {} };
            a.modifications[juce::String::fromUTF8 ("修飾/3")] = { "", parseJson (R"({"changesets":[]})") };

            const auto json = archive::write (a);
            const auto root = juce::JSON::parse (json);
            expectEquals (root.getProperty ("format", {}).toString(), juce::String ("gliss-ara"));
            expectEquals ((int) root.getProperty ("version", 0), 1);
            const auto entryB = root.getProperty ("modifications", {}).getProperty ("mod \"B\"", {});
            expect (entryB.isObject() && entryB.hasProperty ("archive") && entryB.getProperty ("archive", 1).isVoid(),
                    "an archive without edits is written as null");

            DocumentArchive b;
            juce::String error;
            expect (archive::read (json, b, error), error);
            expectEquals (b.workKey, a.workKey);
            expectEquals (b.guide, a.guide);
            expectEquals ((int) b.modifications.size(), 3);
            expectEquals (b.modifications["mod \"B\""].name, juce::String ("ガイド\\1"));
            expect (! b.modifications["mod \"B\""].archive.isObject());
            expectEquals (juce::JSON::toString (b.modifications["mod A"].archive, true), juce::JSON::toString (a.modifications["mod A"].archive, true));
            expect (b.modifications[juce::String::fromUTF8 ("修飾/3")].archive.isObject());
        }

        beginTest ("archive without a guide writes null and reads empty");
        {
            DocumentArchive a;
            a.workKey = "k";
            const auto root = juce::JSON::parse (archive::write (a));
            expect (root.getProperty ("document", {}).getProperty ("guide", 1).isVoid());
            DocumentArchive b;
            juce::String error;
            expect (archive::read (archive::write (a), b, error));
            expect (b.guide.isEmpty() && b.modifications.empty());
        }

        beginTest ("archive refuses other formats and newer versions");
        {
            DocumentArchive out;
            juce::String error;
            expect (! archive::read ("not json", out, error));
            expect (! archive::read (R"({"format":"gliss-ara-document","version":1,"audioModifications":[]})", out, error));
            expect (! archive::read (R"({"format":"gliss-ara","version":2,"document":{},"modifications":{}})", out, error));
            expect (error.contains ("version"));
            expect (! archive::read (R"({"format":"gliss-ara","version":"1"})", out, error));
            expect (archive::read (R"({"format":"gliss-ara","version":1})", out, error), "document and modifications may be missing");
        }

        beginTest ("work keys");
        {
            const auto k1 = archive::makeWorkKey(), k2 = archive::makeWorkKey();
            expect (k1 != k2);
            expect (archive::isValidWorkKey (k1), k1);
            expect (archive::isValidWorkKey ("doc-1_A"));
            expect (! archive::isValidWorkKey (""));
            expect (! archive::isValidWorkKey ("{" + k1 + "}"));
            expect (! archive::isValidWorkKey ("../evil"));
            expect (! archive::isValidWorkKey ("a b"));
            expect (! archive::isValidWorkKey (juce::String::fromUTF8 ("曲")));
            expect (! archive::isValidWorkKey (juce::String::repeatedString ("a", 65)));
            expect (archive::isValidWorkKey (juce::String::repeatedString ("a", 64)));
        }

        beginTest ("source keys are stable 16-digit hex names");
        {
            const auto a = archive::sourceKey ("audioSourceTestPersistentID 0");
            expectEquals (a, archive::sourceKey ("audioSourceTestPersistentID 0"));
            expectEquals (a.length(), 16);
            expect (a.containsOnly ("0123456789abcdef"), a);
            expect (a != archive::sourceKey ("audioSourceTestPersistentID 1"));
            expectEquals (archive::sourceKey ("").length(), 16);
            expectEquals (archive::sourceKey (juce::String::fromUTF8 ("ソース")).length(), 16);
        }
    }
};

class AraRegionTests final : public juce::UnitTest
{
public:
    AraRegionTests() : juce::UnitTest ("ARA region time mapping", "Gliss") {}

    void runTest() override
    {
        const ScopedStdoutLogger logger;

        beginTest ("song <-> modification seconds");
        {
            const auto r = region ("r1", 10.0, 14.0, 2.0, 6.0);
            expect (! regions::songToMod (r, 9.99).has_value());
            expectWithinAbsoluteError (*regions::songToMod (r, 10.0), 2.0, 1.0e-12);
            expectWithinAbsoluteError (*regions::songToMod (r, 13.5), 5.5, 1.0e-12);
            expect (! regions::songToMod (r, 14.0).has_value());

            // 同じ修飾を 2 か所に置いた（DAW で複製した）: 指定の region を優先、無ければソングで最初のもの
            const std::vector<RegionTimes> list { region ("late", 30.0, 34.0, 2.0, 6.0), r };
            expectWithinAbsoluteError (*regions::modToSong (list, 3.0), 11.0, 1.0e-12);
            expectWithinAbsoluteError (*regions::modToSong (list, 3.0, "late"), 31.0, 1.0e-12);
            expectWithinAbsoluteError (*regions::modToSong (list, 3.0, "unknown"), 11.0, 1.0e-12);
            expect (! regions::modToSong (list, 1.0).has_value(), "outside every region");
            expect (! regions::modToSong ({}, 1.0).has_value());
        }

        beginTest ("track offset uses the earliest region");
        {
            expect (! regions::trackOffset ({}).has_value());
            expectEquals (regions::representative ({}), -1);
            const std::vector<RegionTimes> list { region ("b", 30.0, 32.0, 4.0, 6.0), region ("a", 10.0, 12.0, 2.5, 4.5) };
            expectEquals (regions::representative (list), 1);
            expectWithinAbsoluteError (*regions::trackOffset (list), 7.5, 1.0e-12);
        }

        beginTest ("block slices at the same rate match the stage-1 renderer");
        {
            const double rate = 44100.0;
            const RegionTimes cases[] { region ("a", 0.0, 5.0, 0.0, 5.0),
                                        region ("b", 1.25, 3.0, 0.5, 2.25),
                                        region ("c", 2.0, 4.0, 1.0, 2.999),           // 修飾の方が短い（丸めのずれ）
                                        region ("d", 0.333333, 0.9, 0.1, 0.666667) };

            for (const auto& r : cases)
                for (juce::int64 start = -4096; start < (juce::int64) (5.5 * rate); start += 1531)
                    for (int n : { 1, 64, 2048 })
                    {
                        const auto a = regions::sliceBlock (r, start, n, rate, rate);
                        const auto b = stage1Slice (r, start, n, rate);
                        expect (a.numSamples == b.numSamples && (a.isEmpty() || (a.destStart == b.destStart && a.startInSource == b.startInSource)),
                                r.id + " @" + juce::String (start) + "+" + juce::String (n));
                    }
        }

        beginTest ("block slices at another rate continue smoothly");
        {
            const auto r = region ("r", 1.0, 4.0, 0.5, 3.5);
            const double hostRate = 48000.0, sourceRate = 44100.0;
            const int n = 512;
            juce::int64 covered = 0;
            bool first = true;
            double expectedNext = 0.0;
            double worst = 0.0;

            for (juce::int64 start = 0; start < (juce::int64) (5.0 * hostRate); start += n)
            {
                const auto s = regions::sliceBlock (r, start, n, hostRate, sourceRate);
                if (s.isEmpty())
                    continue;

                if (first)
                {
                    expectEquals (start + s.destStart, regions::samplePosition (1.0, hostRate));
                    expectEquals (s.startInSource, regions::samplePosition (0.5, sourceRate));
                    first = false;
                }
                else
                {
                    worst = juce::jmax (worst, std::abs ((double) s.startInSource - expectedNext));
                }

                expectedNext = (double) s.startInSource + s.numSamples * sourceRate / hostRate;
                covered += s.numSamples;
            }

            expect (worst <= 1.0, "the next block starts where RegionReader expects it (±2 is allowed): " + juce::String (worst));
            expect (std::abs ((double) covered - 3.0 * hostRate) <= 1.0, juce::String (covered));
            expect (regions::sliceBlock (r, 0, 0, hostRate, sourceRate).isEmpty());
            expect (regions::sliceBlock (r, 0, 64, 0.0, sourceRate).isEmpty());
        }
    }
};

class AraPlayheadTests final : public juce::UnitTest
{
public:
    AraPlayheadTests() : juce::UnitTest ("ARA playhead", "Gliss") {}

    void runTest() override
    {
        const ScopedStdoutLogger logger;

        beginTest ("loop points in PPQ become seconds");
        {
            expectWithinAbsoluteError (*playhead::ppqToSeconds (8.0, 4.0, 2.0, 120.0), 4.0, 1.0e-12);
            expectWithinAbsoluteError (*playhead::ppqToSeconds (0.0, 4.0, 2.0, 120.0), 0.0, 1.0e-12);
            expect (! playhead::ppqToSeconds (1.0, 0.0, 0.0, 0.0).has_value());

            juce::AudioPlayHead::PositionInfo info;
            info.setTimeInSeconds (2.0);
            info.setPpqPosition (4.0);
            info.setBpm (120.0);
            info.setIsPlaying (true);
            info.setIsLooping (true);
            info.setLoopPoints (juce::AudioPlayHead::LoopPoints { 2.0, 10.0 });

            PlayheadState state;
            expect (! state.read().valid);
            state.write (info);
            const auto s = state.read();
            expect (s.valid && s.playing && s.looping);
            expectWithinAbsoluteError (s.songSec, 2.0, 1.0e-12);
            expectWithinAbsoluteError (s.loopStartSec, 1.0, 1.0e-12);
            expectWithinAbsoluteError (s.loopEndSec, 5.0, 1.0e-12);

            info.setIsLooping (false);
            info.setIsPlaying (false);
            info.setTimeInSeconds (3.5);
            state.write (info);
            expect (! state.read().looping && ! state.read().playing);

            juce::AudioPlayHead::PositionInfo noTime;
            state.write (noTime);
            expectWithinAbsoluteError (state.read().songSec, 3.5, 1.0e-12, "a position without time keeps the last value");
        }

        beginTest ("playhead event maps the song position into each track");
        {
            PlayheadSnapshot s;
            s.valid = true;
            s.songSec = 11.0;
            s.playing = true;

            const std::vector<std::pair<juce::String, std::vector<RegionTimes>>> tracks {
                { "t1", { region ("a", 10.0, 14.0, 2.0, 6.0), region ("b", 10.5, 12.0, 0.0, 1.5) } },   // 重なり: 後に始まった b
                { "t2", { region ("c", 20.0, 21.0, 0.0, 1.0) } },
                { "", { region ("d", 0.0, 100.0, 0.0, 100.0) } } };                                     // track_id がまだ無い

            const auto e = playhead::describe (s, tracks);
            expectWithinAbsoluteError ((double) e.getProperty ("song_sec", 0.0), 11.0, 1.0e-12);
            expect ((bool) e.getProperty ("playing", false));
            expect (e.getProperty ("loop", 1).isVoid());
            const auto mapped = e.getProperty ("mapped", {});
            expectWithinAbsoluteError ((double) mapped.getProperty ("t1", -1.0), 0.5, 1.0e-12);
            expect (mapped.hasProperty ("t2") && mapped.getProperty ("t2", 1).isVoid());
            expectEquals (mapped.getDynamicObject()->getProperties().size(), 2);

            s.looping = true;
            s.loopStartSec = 1.0;
            s.loopEndSec = 2.0;
            const auto loop = playhead::describe (s, {}).getProperty ("loop", {});
            expect (loop.isArray() && loop.size() == 2 && (double) loop[1] == 2.0);
        }
    }
};

class AraEngineCallTests final : public juce::UnitTest
{
public:
    AraEngineCallTests() : juce::UnitTest ("ARA engine calls", "Gliss") {}

    void runTest() override
    {
        const ScopedStdoutLogger logger;

        beginTest ("forbidden tools");
        {
            for (auto* tool : { "new_project", "load_project", "open_project", "save_project", "close_project", "add_track",
                                "remove_track", "export_wav", "render_tracks", "split_track", "join_track", "mute_track_range",
                                "ara_open", "ara_render_dirty" })
                expect (tools::isForbidden (tool), tool);

            for (auto* tool : { "list_tracks", "select_track", "analyze_take", "shift_pitch", "undo", "engine_info", "set_guide_track" })
                expect (! tools::isForbidden (tool), tool);
        }

        beginTest ("which successful calls ask for a sync");
        {
            const auto ok = parseJson (R"({"ok":true})");
            for (auto* tool : { "shift_pitch", "undo", "redo", "apply_plan", "set_pitch_curve", "split_note", "select_track",
                                "analyze_take", "set_guide_track", "set_lyrics", "some_future_tool" })
                expect (tools::shouldSyncAfter (tool, ok), tool);

            for (auto* tool : { "list_tracks", "list_notes", "get_pitch", "export_view_data", "track_overview", "engine_info",
                                "prep_status", "render_audition", "plan_edit", "project_status" })
                expect (! tools::shouldSyncAfter (tool, ok), tool);

            expect (! tools::shouldSyncAfter ("shift_pitch", parseJson (R"({"ok":false,"error":"x"})")));
            expect (! tools::shouldSyncAfter ("get_job", parseJson (R"({"ok":true,"status":"running"})")));
            expect (tools::shouldSyncAfter ("get_job", parseJson (R"({"ok":true,"status":"done"})")));
            expect (isFailure ({}) && isFailure (parseJson (R"({"ok":false})")) && ! isFailure (parseJson (R"({"rev":"x"})")));
            expectEquals (failureReason (parseJson (R"({"ok":false,"error":"boom"})")), juce::String ("boom"));
        }

        beginTest ("GLISS_TEST_EDIT forms");
        {
            auto e = TestEdit::parse (R"({"cents":100,"start_sec":0,"end_sec":5})");
            expect (e.has_value() && e->tool == "shift_pitch" && (int) e->args.getProperty ("cents", 0) == 100);

            e = TestEdit::parse (R"( {"tool":"set_pitch_curve","args":{"points":[[0,1]]}} )");
            expect (e.has_value() && e->tool == "set_pitch_curve" && e->args.getProperty ("points", {}).isArray());

            e = TestEdit::parse (R"({"tool":"undo"})");
            expect (e.has_value() && e->tool == "undo" && e->args.isObject());

            e = TestEdit::parse ("shift_pitch:n001:-150");
            expect (e.has_value() && e->args.getProperty ("note_id", {}).toString() == "n001"
                    && (double) e->args.getProperty ("cents", 0.0) == -150.0);

            for (auto* bad : { "", "  ", "{", "[1,2]", "shift_pitch:n001", "rename:n001:3", R"({"tool":""})" })
                expect (! TestEdit::parse (bad).has_value(), bad);
        }

        beginTest ("ara_render_dirty results");
        {
            DirtyUpdate u;
            juce::String error;
            expect (DirtyUpdate::parse (parseJson (R"({"ok":true,"rev":"a:b~3","reset":false,"more":true,"analysis_pending":false,
                "sr":44100,"channels":2,"source_frames":1000,"restore":[[10,5],[100,20]],
                "windows":[{"start_frame":10,"frames":5,"byte_offset":0},{"start_frame":100,"frames":20,"byte_offset":40}],
                "path":"C:/w/ara-out/x-000001.f32"})"), u, error), error);
            expect (u.rev == "a:b~3" && u.more && ! u.reset && u.sampleRate == 44100.0 && u.numChannels == 2 && u.sourceFrames == 1000);
            expect (u.restore.size() == 2 && u.restore[1].getStart() == 100 && u.restore[1].getLength() == 20);
            expect (u.windows.size() == 2 && u.windows[1].byteOffset == 40 && u.windows[1].frames == 20);
            expectEquals (u.path.getFileName(), juce::String ("x-000001.f32"));

            expect (DirtyUpdate::parse (parseJson (R"({"ok":true,"rev":"empty","reset":true,"sr":48000,"channels":1,"restore":[],"windows":[],"path":null,"analysis_pending":true})"), u, error));
            expect (u.windows.empty() && u.path == juce::File() && u.analysisPending && u.reset);

            expect (! DirtyUpdate::parse (parseJson (R"({"ok":false,"error":"no project"})"), u, error) && error == "no project");
            expect (! DirtyUpdate::parse (parseJson (R"({"ok":true,"sr":48000,"channels":1})"), u, error), "rev is required");
            expect (! DirtyUpdate::parse (parseJson (R"({"ok":true,"rev":"r","sr":48000,"channels":1,"windows":[{"start_frame":0,"frames":4,"byte_offset":0}]})"), u, error),
                    "windows need a path");
            expect (! DirtyUpdate::parse (parseJson (R"({"ok":true,"rev":"r","sr":48000,"channels":1,"restore":[[1]]})"), u, error));
        }
    }
};

class AraFilesTests final : public juce::UnitTest
{
public:
    AraFilesTests() : juce::UnitTest ("ARA source WAV and plugin state", "Gliss") {}

    void runTest() override
    {
        const ScopedStdoutLogger logger;

        const juce::TemporaryFile tempDir;
        const auto dir = tempDir.getFile();
        dir.createDirectory();

        beginTest ("float WAV keeps the host samples exactly");
        {
            const auto file = dir.getChildFile ("source.wav");
            juce::AudioBuffer<float> source (2, 70000);
            juce::Random random (7);
            for (int c = 0; c < 2; ++c)
                for (int i = 0; i < source.getNumSamples(); ++i)
                    source.setSample (c, i, random.nextFloat() * 2.0f - 1.0f);
            source.setSample (0, 5, 1.0e-30f);
            source.setSample (1, 6, -1.5f);   // float の WAV は 1 を超える値もそのまま

            FloatWavWriter writer;
            expect (writer.open (file, 44100.0, 2));
            expect (writer.write (source.getArrayOfReadPointers(), 65536));
            const float* rest[] { source.getReadPointer (0, 65536), source.getReadPointer (1, 65536) };
            expect (writer.write (rest, source.getNumSamples() - 65536));
            expect (writer.finish());
            expectEquals (writer.getNumSamplesWritten(), (juce::int64) source.getNumSamples());

            juce::WavAudioFormat wav;
            std::unique_ptr<juce::AudioFormatReader> reader (wav.createReaderFor (file.createInputStream().release(), true));
            expect (reader != nullptr);

            if (reader != nullptr)
            {
                expectEquals ((int) reader->numChannels, 2);
                expectEquals (reader->sampleRate, 44100.0);
                expectEquals (reader->lengthInSamples, (juce::int64) source.getNumSamples());
                expect (reader->usesFloatingPointData);
                juce::AudioBuffer<float> back (2, source.getNumSamples());
                reader->read (&back, 0, back.getNumSamples(), 0, true, true);
                bool same = true;
                for (int c = 0; c < 2; ++c)
                    same = same && std::memcmp (back.getReadPointer (c), source.getReadPointer (c), sizeof (float) * (size_t) source.getNumSamples()) == 0;
                expect (same, "the samples read back are bit-identical");
            }

            expect (file.getSize() == 4 + 4 + 4 + (8 + 16) + (8 + 4) + 8 + 2 * 4 * (juce::int64) source.getNumSamples());
            FloatWavWriter bad;
            expect (! bad.open (file, 44100.0, 0));
            expect (! bad.write (source.getArrayOfReadPointers(), 4));
            expect (! bad.finish());
        }

        beginTest ("plugin state: shallow merge, null removes, saved and loaded");
        {
            const auto base = parseJson (R"({"keys":{"a":1},"grid":"1/8","view":[0,10]})");
            const auto merged = pluginstate::merge (base, parseJson (R"({"grid":"1/16","view":null,"f0Estimator":"praat"})"));
            expectEquals (merged.getProperty ("grid", {}).toString(), juce::String ("1/16"));
            expect (! merged.hasProperty ("view"));
            expectEquals ((int) merged.getProperty ("keys", {}).getProperty ("a", 0), 1);
            expectEquals (base.getProperty ("grid", {}).toString(), juce::String ("1/8"), "the base is not changed");

            const auto file = dir.getChildFile ("sub").getChildFile ("plugin-state.json");
            expect (pluginstate::load (file).isObject(), "missing file reads as an empty object");
            expect (pluginstate::save (file, merged));
            expectEquals (juce::JSON::toString (pluginstate::load (file), true), juce::JSON::toString (merged, true));
            file.replaceWithText ("{broken");
            expect (pluginstate::load (file).isObject() && pluginstate::load (file).getDynamicObject()->getProperties().isEmpty());
        }
    }
};

class AraDocumentSyncTests final : public juce::UnitTest
{
public:
    AraDocumentSyncTests() : juce::UnitTest ("ARA document sync without an engine", "Gliss") {}

    void runTest() override
    {
        const ScopedStdoutLogger logger;

        beginTest ("a disabled engine settles and refuses calls");
        {
            DocumentSync::Options options;
            options.engineDisabled = true;
            juce::StringArray events;
            std::mutex m;
            DocumentSync::Callbacks callbacks;
            callbacks.event = [&] (const juce::String& eventName, const juce::var&) { std::lock_guard g (m); events.add (eventName); };

            DocumentSync sync (EngineConfig {}, options, callbacks);
            SyncModel model;
            model.workKey = "k1";
            SyncSource s;
            s.id = "src";
            s.sampleRate = 44100.0;
            s.numChannels = 1;
            s.numSamples = 100;
            s.generation = 1;
            s.samplesAvailable = true;
            model.sources.push_back (s);
            SyncModification mod;
            mod.araId = "mod";
            mod.sourceId = "src";
            mod.pcm = std::make_shared<EditedPcm> (44100.0, 1);
            model.modifications.push_back (mod);
            sync.setModel (model);

            for (int i = 0; i < 200 && ! sync.isSettled(); ++i)
                juce::Thread::sleep (10);

            expect (sync.isSettled());
            expectEquals (sync.getEngineStatus().state, juce::String ("disabled"));
            expect (isFailure (sync.callTool ("list_tracks", {}, 1000)));
            expect (! sync.hasOpened());
            expect (sync.getWorkDir() == juce::File());
        }

        beginTest ("pending restores and guides are what gets stored until applied");
        {
            DocumentSync::Options options;
            options.engineDisabled = true;
            DocumentSync sync (EngineConfig {}, options, {});

            expect (sync.getArchiveForStore ("mod").isVoid());
            const auto archive = parseJson (R"({"changesets":[{"id":"c001"}]})");
            sync.setPendingRestore ("mod", archive);
            sync.setPendingGuide ("guide-mod");
            expectEquals (juce::JSON::toString (sync.getArchiveForStore ("mod"), true), juce::JSON::toString (archive, true));
            expect (sync.getArchiveForStore ("other").isVoid());
            expectEquals (sync.getGuideForStore(), juce::String ("guide-mod"));

            // エンジンが動いていなければ、取り直しは何もしない（待たない）
            const auto start = juce::Time::getMillisecondCounter();
            sync.refreshArchives (5000);
            expect (juce::Time::getMillisecondCounter() - start < 1000);
            sync.shutdown();
            sync.shutdown();
        }

        beginTest ("an engine that cannot start reports failed and settles");
        {
            EngineConfig missing;
            missing.executable = juce::File::getSpecialLocation (juce::File::tempDirectory).getChildFile ("gliss-no-such-engine.exe");
            missing.workingDirectory = juce::File::getSpecialLocation (juce::File::tempDirectory);

            juce::StringArray states;
            std::mutex m;
            DocumentSync::Callbacks callbacks;
            callbacks.event = [&] (const juce::String& eventName, const juce::var& data)
            {
                if (eventName == "engine")
                {
                    std::lock_guard g (m);
                    states.add (data.getProperty ("state", {}).toString());
                }
            };

            DocumentSync sync (missing, {}, callbacks);
            sync.requestEngine();
            expect (! sync.waitForEngine (20000));
            expectEquals (sync.getEngineStatus().state, juce::String ("failed"));

            for (int i = 0; i < 200 && ! sync.isSettled(); ++i)
                juce::Thread::sleep (10);

            expect (sync.isSettled());
            std::lock_guard g (m);
            expect (states.contains ("starting") && states.contains ("failed"), states.joinIntoString (","));
        }
    }
};

static AraArchiveTests araArchiveTests;
static AraRegionTests araRegionTests;
static AraPlayheadTests araPlayheadTests;
static AraEngineCallTests araEngineCallTests;
static AraFilesTests araFilesTests;
static AraDocumentSyncTests araDocumentSyncTests;

} // namespace gliss
