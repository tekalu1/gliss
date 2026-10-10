#include "PlayheadState.h"

namespace gliss
{

void PlayheadState::write (const juce::AudioPlayHead::PositionInfo& position, std::uintptr_t source) noexcept
{
    const auto seconds = position.getTimeInSeconds();
    const auto isPlaying = position.getIsPlaying();
    if (! seconds.hasValue() && isPlaying)
        return;

    bool expected = false;
    if (! writing.compare_exchange_strong (expected, true, std::memory_order_acquire))
        return; // 複数の processor が同時に呼んでもオーディオスレッドでは待たない

    const auto now = juce::Time::getMillisecondCounter();
    const auto stale = (juce::uint32) (now - stampMs.load()) > 250;
    if (masterSource != 0 && masterSource != source && ! stale
        && (playing.load() || ! isPlaying))
    {
        writing.store (false, std::memory_order_release);
        return;
    }
    masterSource = source;
    const auto odd = sequence.fetch_add (1) + 1;

    if (seconds.hasValue())
        songSec.store (*seconds);
    playing.store (isPlaying);

    bool loopOn = false;

    if (position.getIsLooping())
    {
        const auto loop = position.getLoopPoints();
        const auto ppq = position.getPpqPosition();
        const auto bpm = position.getBpm();

        if (seconds.hasValue() && loop.hasValue() && ppq.hasValue() && bpm.hasValue())
        {
            const auto a = playhead::ppqToSeconds (loop->ppqStart, *ppq, *seconds, *bpm);
            const auto b = playhead::ppqToSeconds (loop->ppqEnd, *ppq, *seconds, *bpm);

            if (a.has_value() && b.has_value() && *b > *a)
            {
                loopStartSec.store (*a);
                loopEndSec.store (*b);
                loopOn = true;
            }
        }
    }

    looping.store (loopOn);
    stampMs.store (now);
    valid.store (true);
    sequence.store (odd + 1);
    writing.store (false, std::memory_order_release);
}

PlayheadSnapshot PlayheadState::read() const noexcept
{
    PlayheadSnapshot s;
    for (int attempt = 0; attempt < 8; ++attempt)
    {
        const auto before = sequence.load();
        if (before & 1)
            continue;
        s.valid = valid.load();
        s.songSec = songSec.load();
        s.playing = playing.load();
        s.looping = looping.load();
        s.loopStartSec = loopStartSec.load();
        s.loopEndSec = loopEndSec.load();
        s.stampMs = stampMs.load();
        if (sequence.load() == before)
        {
            s.sequence = before / 2;
            return s;
        }
    }
    s.valid = false;
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
    o->setProperty ("sequence", (juce::int64) s.sequence);
    o->setProperty ("stamp_ms", (juce::int64) s.stampMs);

    if (s.looping)
        o->setProperty ("loop", juce::Array<juce::var> { s.loopStartSec, s.loopEndSec });
    else
        o->setProperty ("loop", juce::var());

    o->setProperty ("mapped", juce::var (mapped));
    return juce::var (o);
}

} // namespace playhead
} // namespace gliss
