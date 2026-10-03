// 段階 4（DAW に返すノート）のうち、ARA のホストもエンジンも要らない部分の単体テスト:
// ara_notes の写し・ソースの秒からソングの秒への変換・リージョンでの切り取り・時間の範囲・品質のラベル。
// エンジンにつないだ通し（解析の後と編集の後に PlaybackRegion のノートがエンジンと一致する）は plugin/tests/aratest の GlissARATest。
#include "ara/NoteContent.h"

#include <juce_core/juce_core.h>

#include <cstdio>

namespace gliss
{

namespace
{
/** テストの間だけ、JUCE のログ（失敗の文を含む）を標準出力に出す。 */
struct NoteTestLogger final : public juce::Logger
{
    NoteTestLogger() : previous (juce::Logger::getCurrentLogger()) { juce::Logger::setCurrentLogger (this); }
    ~NoteTestLogger() override { juce::Logger::setCurrentLogger (previous); }
    void logMessage (const juce::String& message) override { std::printf ("%s\n", message.toRawUTF8()); std::fflush (stdout); }
    juce::Logger* previous;
};

RegionTimes regionAt (double songStart, double modStart, double modEnd)
{
    RegionTimes r;
    r.id = "r";
    r.songStart = songStart;
    r.songEnd = songStart + (modEnd - modStart);
    r.modStart = modStart;
    r.modEnd = modEnd;
    return r;
}

/** ara_notes の返り値の形（エンジンの丸めと同じく、秒は 6 桁・Hz は 4 桁）。 */
const char* sampleResult = R"({
  "ok": true,
  "notes": {
    "mod-A": {"track": "t1", "rev": "aaaa:bbbb", "state": "ready", "edited": true,
              "notes": [{"id": "n2", "start_sec": 1.65, "end_sec": 2.2, "hz": 349.2282, "midi": 65.0, "volume": 0.8},
                        {"id": "n1", "start_sec": 0.5, "end_sec": 1.05, "hz": 261.6256, "midi": 60.0, "volume": 0.75},
                        {"id": "n3", "start_sec": 2.8, "end_sec": 3.35, "hz": 392.0, "midi": 67.0, "volume": 1.5},
                        {"id": "bad", "start_sec": 3.5, "end_sec": 3.5, "hz": 100.0, "midi": 43.0, "volume": 0.5}],
              "source_notes": [{"id": "n1", "start_sec": 0.5, "end_sec": 1.05, "hz": 261.6256, "midi": 60.0, "volume": 0.75},
                               {"id": "n2", "start_sec": 1.65, "end_sec": 2.2, "hz": 329.6276, "midi": 64.0, "volume": 0.8},
                               {"id": "n3", "start_sec": 2.8, "end_sec": 3.35, "hz": 392.0, "midi": 67.0, "volume": -1}]},
    "mod-B": {"track": "t2", "rev": "empty", "state": "pending", "edited": false, "notes": [], "source_notes": []},
    "mod-C": {"track": "t3", "rev": "cccc:dddd", "state": "ready", "edited": false,
              "notes": [{"id": "n1", "start_sec": 0.25, "end_sec": 0.75, "hz": 440.0, "midi": 69.4, "volume": 0.5}],
              "source_notes": [{"id": "n1", "start_sec": 0.25, "end_sec": 0.75, "hz": 440.0, "midi": 69.4, "volume": 0.5}]},
    "broken": 3
  }
})";
} // namespace

class NoteContentTests final : public juce::UnitTest
{
public:
    NoteContentTests() : juce::UnitTest ("ARA notes for the DAW", "Gliss") {}

    void runTest() override
    {
        const NoteTestLogger logger;
        const auto parsed = notes::parse (juce::JSON::parse (juce::String (sampleResult)));

        beginTest ("ara_notes is read into per-modification copies, sorted by start, broken entries skipped");
        {
            expectEquals ((int) parsed.size(), 3);
            const auto& a = parsed.at ("mod-A");
            expect (a.ready && a.edited);
            expectEquals (a.rev, juce::String ("aaaa:bbbb"));
            expectEquals ((int) a.notes.size(), 3, "a zero-length note is dropped");
            expectEquals (a.notes[0].startPosition, 0.5);
            expectEquals (a.notes[1].startPosition, 1.65);
            expectEquals (a.notes[0].noteDuration, 1.05 - 0.5);
            expectEquals (a.notes[0].signalDuration, a.notes[0].noteDuration);
            expectEquals (a.notes[0].attackDuration, 0.0);
            expectEquals (a.notes[1].pitchNumber, 65);
            expectWithinAbsoluteError (a.notes[1].frequency, 349.2282f, 1.0e-3f);
            expectEquals (a.notes[2].volume, 1.0f, "volume is clamped to 0..1");
            expectEquals (a.sourceNotes[2].volume, 0.0f);
            expectEquals (a.sourceNotes[1].pitchNumber, 64);
            expectEquals (parsed.at ("mod-C").notes[0].pitchNumber, 69, "the pitch number is the rounded MIDI value");

            const auto& b = parsed.at ("mod-B");
            expect (! b.ready && b.notes.empty());
            expect (notes::parse (juce::var()).empty());
        }

        beginTest ("grade: initial before the analysis, detected after it, adjusted after an edit; the source is never adjusted");
        {
            expect (notes::grade (nullptr) == NoteGrade::initial);
            expect (notes::grade (&parsed.at ("mod-B")) == NoteGrade::initial);
            expect (notes::grade (&parsed.at ("mod-C")) == NoteGrade::detected);
            expect (notes::grade (&parsed.at ("mod-A")) == NoteGrade::adjusted);
            expect (notes::grade (&parsed.at ("mod-A"), true) == NoteGrade::detected);
            expectEquals ((int) NoteGrade::detected, 1, "same value as kARAContentGradeDetected");
            expectEquals ((int) NoteGrade::adjusted, 2, "same value as kARAContentGradeAdjusted");
        }

        beginTest ("modification and source readers use source seconds; a pending copy gives nothing");
        {
            const auto& a = parsed.at ("mod-A");
            expectEquals ((int) notes::forModification (a).size(), 3);
            expectEquals ((int) notes::forSource (a).size(), 3);
            expectEquals (notes::forSource (a)[1].pitchNumber, 64, "the source has the analysis only");
            expectEquals (notes::forModification (a)[1].pitchNumber, 65);
            expect (notes::forModification (parsed.at ("mod-B")).empty());
            expect (notes::forRegion (parsed.at ("mod-B"), regionAt (0.0, 0.0, 10.0)).empty());
        }

        beginTest ("time range: notes that intersect at least partly; the end of the range is excluded");
        {
            const auto& a = parsed.at ("mod-A");
            const auto ids = [] (const std::vector<NoteEvent>& v)
            {
                juce::StringArray s;
                for (const auto& e : v)
                    s.add (juce::String (e.pitchNumber));
                return s.joinIntoString (",");
            };

            expectEquals (ids (notes::forModification (a, NoteTimeRange { 1.0, 1.0 })), juce::String ("60,65"));
            expectEquals (ids (notes::forModification (a, NoteTimeRange { 1.05, 0.7 })), juce::String ("65"), "a note ending at the start is out");
            expectEquals (ids (notes::forModification (a, NoteTimeRange { 0.0, 0.5 })), juce::String (""), "a note starting at the end is out");
            expectEquals (ids (notes::forModification (a, NoteTimeRange { 0.5, 0.0 })), juce::String ("60"), "an empty range is a point");
            expectEquals (ids (notes::forModification (a, NoteTimeRange { 3.0, 100.0 })), juce::String ("67"));
        }

        beginTest ("region: notes move to song seconds by (song start - modification start), exactly");
        {
            const auto& a = parsed.at ("mod-A");
            const auto atZero = notes::forRegion (a, regionAt (0.0, 0.0, 6.2));
            expectEquals ((int) atZero.size(), 3);
            for (size_t i = 0; i < atZero.size(); ++i)
                expect (juce::exactlyEqual (atZero[i].startPosition, a.notes[i].startPosition)
                        && juce::exactlyEqual (atZero[i].noteDuration, a.notes[i].noteDuration), "a region at 0 is the modification as is");

            const auto moved = notes::forRegion (a, regionAt (10.0, 0.0, 6.2));
            expectWithinAbsoluteError (moved[0].startPosition, 10.5, 1.0e-12);
            expectWithinAbsoluteError (moved[2].startPosition, 12.8, 1.0e-12);
            expectEquals (moved[1].noteDuration, a.notes[1].noteDuration);
            expectEquals (moved[1].frequency, a.notes[1].frequency);
            expectEquals (moved[1].volume, a.notes[1].volume);
        }

        beginTest ("region: trimmed to the region's range in the modification; notes outside are dropped");
        {
            const auto& a = parsed.at ("mod-A");
            // ソースの 0.8〜3.0 秒をソングの 20 秒に置く: 1 つ目は頭が切れ、3 つ目は尻が切れる
            const auto r = notes::forRegion (a, regionAt (20.0, 0.8, 3.0));
            expectEquals ((int) r.size(), 3);
            expectWithinAbsoluteError (r[0].startPosition, 20.0, 1.0e-12);
            expectWithinAbsoluteError (r[0].noteDuration, 1.05 - 0.8, 1.0e-12);
            expectWithinAbsoluteError (r[0].signalDuration, 1.05 - 0.8, 1.0e-12);
            expectWithinAbsoluteError (r[1].startPosition, 20.0 + 1.65 - 0.8, 1.0e-12);
            expectWithinAbsoluteError (r[1].noteDuration, 2.2 - 1.65, 1.0e-12);
            expectWithinAbsoluteError (r[2].startPosition, 20.0 + 2.8 - 0.8, 1.0e-12);
            expectWithinAbsoluteError (r[2].noteDuration, 3.0 - 2.8, 1.0e-12);
            expectEquals (r[0].pitchNumber, 60, "a trimmed note keeps its pitch");

            const auto onlyMiddle = notes::forRegion (a, regionAt (0.0, 1.2, 2.5));
            expectEquals ((int) onlyMiddle.size(), 1);
            expectEquals (onlyMiddle[0].pitchNumber, 65);

            expect (notes::forRegion (a, regionAt (0.0, 1.05, 1.65)).empty(), "a gap between notes gives nothing");
            expect (notes::forRegion (a, regionAt (0.0, 3.0, 3.0)).empty(), "an empty region gives nothing");

            // ソングの範囲（content reader の range）はソングの秒で絞る
            const auto ranged = notes::forRegion (a, regionAt (20.0, 0.8, 3.0), NoteTimeRange { 20.9, 0.5 });
            expectEquals ((int) ranged.size(), 1);
            expectEquals (ranged[0].pitchNumber, 65);
        }

        beginTest ("a region longer in the song than in the modification is cut to the shorter length");
        {
            auto r = regionAt (5.0, 0.0, 1.0);
            r.songEnd = 9.0;   // ソングでは 4 秒、修飾では 1 秒（Gliss は伸ばさない）
            const auto out = notes::forRegion (parsed.at ("mod-A"), r);
            expectEquals ((int) out.size(), 1);
            expectWithinAbsoluteError (out[0].getEnd(), 5.0 + 1.0, 1.0e-12);
        }

        beginTest ("content comparison tells a changed copy (notification) from the same copy");
        {
            const auto& a = parsed.at ("mod-A");
            auto same = a;
            same.rev = "other";
            expect (a.sameContent (same) && a.sameSourceContent (same), "only the revision differs");

            auto edited = a;
            edited.notes[1].frequency += 1.0f;
            expect (! a.sameContent (edited) && a.sameSourceContent (edited), "an edit changes the modification, not the source");

            auto unedited = a;
            unedited.edited = false;
            expect (! a.sameContent (unedited), "the grade is part of the content");

            auto reanalyzed = a;
            reanalyzed.sourceNotes.pop_back();
            expect (! a.sameSourceContent (reanalyzed));

            const auto& pending = parsed.at ("mod-B");
            expect (! pending.sameContent (parsed.at ("mod-C")));
        }
    }
};

static NoteContentTests noteContentTests;

} // namespace gliss
