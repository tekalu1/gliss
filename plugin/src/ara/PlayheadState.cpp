#include "PlayheadState.h"

namespace gliss
{

void PlayheadState::write (const juce::AudioPlayHead::PositionInfo& position) noexcept
{
    const auto seconds = position.getTimeInSeconds();

    if (! seconds.hasValue())
        return;

    songSec.store (*seconds, std::memory_order_relaxed);
    playing.store (position.getIsPlaying(), std::memory_order_relaxed);

    bool loopOn = false;

    if (position.getIsLooping())
    {
        const auto loop = position.getLoopPoints();
        const auto ppq = position.getPpqPosition();
        const auto bpm = position.getBpm();

        if (loop.hasValue() && ppq.hasValue() && bpm.hasValue())
        {
            const auto a = playhead::ppqToSeconds (loop->ppqStart, *ppq, *seconds, *bpm);
            const auto b = playhead::ppqToSeconds (loop->ppqEnd, *ppq, *seconds, *bpm);

            if (a.has_value() && b.has_value() && *b > *a)
            {
                loopStartSec.store (*a, std::memory_order_relaxed);
                loopEndSec.store (*b, std::memory_order_relaxed);
                loopOn = true;
            }
        }
    }

    looping.store (loopOn, std::memory_order_relaxed);
    stampMs.store (juce::Time::getMillisecondCounter(), std::memory_order_relaxed);
    valid.store (true, std::memory_order_release);
}

PlayheadSnapshot PlayheadState::read() const noexcept
{
    PlayheadSnapshot s;
    s.valid = valid.load (std::memory_order_acquire);
    s.songSec = songSec.load (std::memory_order_relaxed);
    s.playing = playing.load (std::memory_order_relaxed);
    s.looping = looping.load (std::memory_order_relaxed);
    s.loopStartSec = loopStartSec.load (std::memory_order_relaxed);
    s.loopEndSec = loopEndSec.load (std::memory_order_relaxed);
    s.stampMs = stampMs.load (std::memory_order_relaxed);
    return s;
}

namespace playhead
{

std::optional<double> ppqToSeconds (double ppq, double ppqPosition, double timeSec, double bpm)
{
    if (! (bpm > 0.0))
        return std::nullopt;

    return timeSec + (ppq - ppqPosition) * 60.0 / bpm;
}

juce::var describe (const PlayheadSnapshot& s, const std::vector<std::pair<juce::String, std::vector<RegionTimes>>>& tracks)
{
    auto* mapped = new juce::DynamicObject();

    for (const auto& [trackId, list] : tracks)
    {
        if (trackId.isEmpty())
            continue;

        juce::var value;

        // いま鳴っているリージョン（重なっていればソングで後に始まったもの = 上に置かれたもの）の中の編集の秒。
        const RegionTimes* hit = nullptr;

        for (const auto& r : list)
            if (regions::songToMod (r, s.songSec).has_value() && (hit == nullptr || r.songStart > hit->songStart))
                hit = &r;

        if (hit != nullptr)
            value = *regions::songToMod (*hit, s.songSec);

        mapped->setProperty (juce::Identifier (trackId), value);
    }

    auto* o = new juce::DynamicObject();
    o->setProperty ("song_sec", s.songSec);
    o->setProperty ("playing", s.playing);

    if (s.looping)
        o->setProperty ("loop", juce::Array<juce::var> { s.loopStartSec, s.loopEndSec });
    else
        o->setProperty ("loop", juce::var());

    o->setProperty ("mapped", juce::var (mapped));
    return juce::var (o);
}

} // namespace playhead
} // namespace gliss
