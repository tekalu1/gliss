#pragma once

#include <juce_core/juce_core.h>

#include <cmath>
#include <optional>
#include <vector>

namespace gliss
{

/** 1 つの PlaybackRegion の時間（秒）。song はソング、mod は AudioModification の時間。 */
struct RegionTimes
{
    juce::String id;
    double songStart = 0.0, songEnd = 0.0;
    double modStart = 0.0, modEnd = 0.0;

    double songLength() const noexcept { return songEnd - songStart; }
    double modLength() const noexcept { return modEnd - modStart; }
    double scale() const noexcept { return songLength() > 0.0 ? modLength() / songLength() : 0.0; }
    /** 伸縮しているか。伸縮を有効にしたホストが丸めの誤差ほどの長さの差を渡しても、伸縮の処理（音が変わる）に入らないよう、
        長さの差が 1 マイクロ秒（192 kHz の 1 サンプルより短い）未満なら伸縮していないとみなす。 */
    bool isStretched() const noexcept { return songLength() > 0.0 && std::abs (modLength() - songLength()) > 1.0e-6; }
    void normalize (bool timestretchEnabled) noexcept { if (! timestretchEnabled) modEnd = modStart + songLength(); }

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

/** 伸縮したリージョンのソング上の切り出し。startInModification は修飾の時間をホストのサンプルで数えた位置。 */
struct StretchSlice
{
    int destStart = 0;
    int numSamples = 0;
    double startInModification = 0.0;
    bool isEmpty() const noexcept { return numSamples <= 0; }
};

StretchSlice sliceStretchedBlock (const RegionTimes&, juce::int64 blockStart, int numSamples, double hostRate) noexcept;

/** ARA の samplePositionAtTime と同じ丸め。 */
juce::int64 samplePosition (double seconds, double rate);
} // namespace regions

} // namespace gliss
