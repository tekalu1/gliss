#pragma once

#include <juce_core/juce_core.h>

#include <optional>
#include <vector>

namespace gliss
{

/** 1 つの PlaybackRegion の時間（秒）。song はソング（DAW のタイムライン）、mod は AudioModification（= ソース）の秒。
    Gliss は時間を伸ばさないので、長さは両方で同じ（ずれたら短い方に合わせる）。 */
struct RegionTimes
{
    juce::String id;
    double songStart = 0.0, songEnd = 0.0;
    double modStart = 0.0, modEnd = 0.0;

    double getLength() const noexcept { return juce::jmin (songEnd - songStart, modEnd - modStart); }

    /** 画面に渡す形 { id, song_start, song_end, mod_start, mod_end }。 */
    juce::var toVar() const;
};

namespace regions
{
/** ソングの秒 → その region の中の修飾の秒（region の外なら無し）。 */
std::optional<double> songToMod (const RegionTimes&, double songSec);

/** 修飾の秒 → ソングの秒。その秒を含む region（preferredId のものを優先、無ければソングで最初のもの）で直す。含む region が無ければ無し。 */
std::optional<double> modToSong (const std::vector<RegionTimes>&, double modSec, const juce::String& preferredId = {});

/** エンジンのトラックの offset_sec（代表のリージョン＝ソングで最初のものでソースの 0 秒が置かれるソングの秒）。region が無ければ無し。 */
std::optional<double> trackOffset (const std::vector<RegionTimes>&);

/** 代表のリージョン（ソングで最初のもの）の添字。無ければ -1。 */
int representative (const std::vector<RegionTimes>&);

/** 1 ブロックのうち region が鳴らす部分。 */
struct BlockSlice
{
    int destStart = 0;              // ブロックの中の頭
    int numSamples = 0;
    juce::int64 startInSource = 0;  // ソースのサンプル（ソースの周波数）

    bool isEmpty() const noexcept { return numSamples <= 0; }
};

/** ホストのブロック [blockStart, blockStart + numSamples)（ホストの周波数のサンプル）のうち region が鳴らす部分と、
    そこに当たるソースの位置。サンプルの位置は ARA と同じ丸め（samplePositionAtTime = floor (t * rate + 0.5)）。
    周波数が同じなら段階 1 と同じ整数の計算（ソースの位置 = ソングの位置 + (修飾の頭 − ソングの頭)）。 */
BlockSlice sliceBlock (const RegionTimes&, juce::int64 blockStart, int numSamples, double hostRate, double sourceRate);

/** ARA の samplePositionAtTime と同じ丸め。 */
juce::int64 samplePosition (double seconds, double rate);
} // namespace regions

} // namespace gliss
