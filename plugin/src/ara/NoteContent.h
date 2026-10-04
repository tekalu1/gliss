#pragma once

#include "RegionMapping.h"

#include <juce_core/juce_core.h>

#include <map>
#include <memory>
#include <optional>
#include <vector>

namespace gliss
{

/** DAW に返す 1 つのノート（ARA の ARAContentNote と同じ項目。ARA の型を使わない）。時間は秒。 */
struct NoteEvent
{
    float frequency = 0.0f;             // 中心の音程の Hz
    int pitchNumber = 0;                // MIDI のノート番号（frequency を丸めたもの）
    float volume = 0.0f;                // 0〜1（dB に近い尺度）
    double startPosition = 0.0;         // ソース（修飾）の秒、またはソングの秒
    double attackDuration = 0.0;
    double noteDuration = 0.0;
    double signalDuration = 0.0;

    double getEnd() const noexcept { return startPosition + noteDuration; }
};

/** 品質のラベル（ARA の ARAContentGrade と同じ値）。 */
enum class NoteGrade
{
    initial = 0,                        // 解析がまだ（ノートを出さない）
    detected = 1,                       // 解析だけ（DAW の画面では analyzed と出ることが多い）
    adjusted = 2,                       // 利用者が編集した
};

/** 修飾 1 つのノートの写し（エンジンの ara_notes の 1 行）。同期のスレッドが作り、ARA のスレッドが読むだけ。 */
struct ModificationNotes
{
    juce::String rev;                   // ara_revs と同じ版
    bool ready = false;                 // 解析が済んだ（notes・sourceNotes が使える）
    bool edited = false;                // 編集リストが空でない
    std::vector<NoteEvent> notes;       // 編集を当てた後（ソースの秒・頭の順）
    std::vector<NoteEvent> sourceNotes; // 解析だけ（ソースの秒・頭の順）

    bool sameContent (const ModificationNotes&) const noexcept;
    bool sameSourceContent (const ModificationNotes&) const noexcept;
};

/** content reader に渡す時間の範囲（ARA の ARAContentTimeRange。start の事象は含み、start + duration の事象は含まない）。 */
struct NoteTimeRange
{
    double start = 0.0, duration = 0.0;
};

namespace notes
{
/** ara_notes の返り値 → 修飾ごとの写し。読めない行は飛ばす。 */
std::map<juce::String, ModificationNotes> parse (const juce::var& result);

/** 写しの品質のラベル（写しが無い・解析がまだなら initial）。forSource は AudioSource 用（編集を見ない＝detected）。 */
NoteGrade grade (const ModificationNotes*, bool forSource = false) noexcept;

/** 修飾（ソースの秒）のノート。range があればそれに掛かるものだけ（ARA の決まり: 少しでも重なれば返す）。 */
std::vector<NoteEvent> forModification (const ModificationNotes&, const std::optional<NoteTimeRange>& range = {});

/** AudioSource（ソースの秒）のノート（解析だけ）。 */
std::vector<NoteEvent> forSource (const ModificationNotes&, const std::optional<NoteTimeRange>& range = {});

/** PlaybackRegion（ソングの秒）のノート: リージョンの修飾の範囲で切り、伸縮比に従ってソングの秒に写す。
    songRange はソングの秒。 */
std::vector<NoteEvent> forRegion (const ModificationNotes&, const RegionTimes&, const std::optional<NoteTimeRange>& songRange = {});
} // namespace notes

} // namespace gliss
