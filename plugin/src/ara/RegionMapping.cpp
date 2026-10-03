#include "RegionMapping.h"

#include <cmath>

namespace gliss
{

juce::var RegionTimes::toVar() const
{
    auto* o = new juce::DynamicObject();
    o->setProperty ("id", id);
    o->setProperty ("song_start", songStart);
    o->setProperty ("song_end", songEnd);
    o->setProperty ("mod_start", modStart);
    o->setProperty ("mod_end", modEnd);
    return juce::var (o);
}

namespace regions
{

juce::int64 samplePosition (double seconds, double rate)
{
    return (juce::int64) std::floor (seconds * rate + 0.5);
}

std::optional<double> songToMod (const RegionTimes& r, double songSec)
{
    if (songSec < r.songStart || songSec >= r.songEnd || r.scale() <= 0.0)
        return std::nullopt;

    return r.modStart + (r.isStretched() ? (songSec - r.songStart) * r.scale() : songSec - r.songStart);
}

std::optional<double> modToSong (const std::vector<RegionTimes>& list, double modSec, const juce::String& preferredId)
{
    const auto contains = [modSec] (const RegionTimes& r) { return r.scale() > 0.0 && modSec >= r.modStart && modSec < r.modEnd; };

    if (preferredId.isNotEmpty())
        for (const auto& r : list)
            if (r.id == preferredId && contains (r))
                return r.songStart + (r.isStretched() ? (modSec - r.modStart) / r.scale() : modSec - r.modStart);

    const RegionTimes* best = nullptr;

    for (const auto& r : list)
        if (contains (r) && (best == nullptr || r.songStart < best->songStart))
            best = &r;

    if (best == nullptr)
        return std::nullopt;

    return best->songStart + (best->isStretched() ? (modSec - best->modStart) / best->scale() : modSec - best->modStart);
}

int representative (const std::vector<RegionTimes>& list)
{
    int best = -1;

    for (int i = 0; i < (int) list.size(); ++i)
        if (best < 0 || list[(size_t) i].songStart < list[(size_t) best].songStart)
            best = i;

    return best;
}

std::optional<double> trackOffset (const std::vector<RegionTimes>& list)
{
    const auto i = representative (list);

    if (i < 0)
        return std::nullopt;

    const auto& r = list[(size_t) i];
    if (r.scale() <= 0.0)
        return std::nullopt;
    return r.songStart - (r.isStretched() ? r.modStart / r.scale() : r.modStart);
}

BlockSlice sliceBlock (const RegionTimes& r, juce::int64 blockStart, int numSamples, double hostRate, double sourceRate)
{
    BlockSlice slice;

    if (numSamples <= 0 || hostRate <= 0.0 || sourceRate <= 0.0)
        return slice;

    const auto blockEnd = blockStart + numSamples;

    if (juce::exactlyEqual (hostRate, sourceRate))
    {
        // 段階 1 と同じ: ソングの範囲（ホストのサンプル）と、ソングの頭に置いた修飾の範囲の重なり。
        const auto playStart = samplePosition (r.songStart, hostRate);
        const auto playEnd = samplePosition (r.songEnd, hostRate);
        const auto modStart = samplePosition (r.modStart, sourceRate);
        const auto modEnd = samplePosition (r.modEnd, sourceRate);
        const auto start = juce::jmax (blockStart, playStart);
        const auto end = juce::jmin (blockEnd, playEnd, playStart + (modEnd - modStart));

        if (end <= start)
            return slice;

        slice.destStart = (int) (start - blockStart);
        slice.numSamples = (int) (end - start);
        slice.startInSource = start + (modStart - playStart);
        return slice;
    }

    // 周波数が違う: ソングの範囲はホストのサンプル、ソースの位置は秒を介して直す（丸めの誤差は 0.5 サンプル以内。
    // RegionReader は ±2 サンプルまでを前のブロックの続きと見るので、ブロックごとに丸めても流しの変換が途切れない）。
    const auto playStart = samplePosition (r.songStart, hostRate);
    const auto playEnd = juce::jmin (samplePosition (r.songEnd, hostRate), playStart + samplePosition (r.modLength(), hostRate));
    const auto start = juce::jmax (blockStart, playStart);
    const auto end = juce::jmin (blockEnd, playEnd);

    if (end <= start)
        return slice;

    slice.destStart = (int) (start - blockStart);
    slice.numSamples = (int) (end - start);
    const auto secondsIntoRegion = (double) (start - playStart) / hostRate;
    slice.startInSource = samplePosition (r.modStart + secondsIntoRegion, sourceRate);
    return slice;
}

StretchSlice sliceStretchedBlock (const RegionTimes& r, juce::int64 blockStart, int numSamples, double hostRate) noexcept
{
    StretchSlice slice;
    if (numSamples <= 0 || hostRate <= 0.0 || r.scale() <= 0.0)
        return slice;
    const auto playStart = samplePosition (r.songStart, hostRate);
    const auto start = juce::jmax (blockStart, playStart);
    const auto end = juce::jmin (blockStart + numSamples, samplePosition (r.songEnd, hostRate));
    if (end <= start)
        return slice;
    slice.destStart = (int) (start - blockStart);
    slice.numSamples = (int) (end - start);
    slice.startInModification = r.modStart * hostRate + (double) (start - playStart) * r.scale();
    return slice;
}

} // namespace regions
} // namespace gliss
