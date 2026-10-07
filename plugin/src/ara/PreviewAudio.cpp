#include "PreviewAudio.h"
#include <algorithm>
#include <cmath>

namespace gliss
{

void PreviewAudio::fadeEdges (Clip& clip) noexcept
{
    const auto fade = juce::jmin (clip.frames() / 2, (int) (clip.sampleRate * 0.006));
    for (auto& channel : clip.channels)
        for (int i = 0; i < fade; ++i)
        {
            const auto gain = (float) i / (float) juce::jmax (1, fade);
            channel[(size_t) i] *= gain;
            channel[channel.size() - 1 - (size_t) i] *= gain;
        }
}

void PreviewAudio::collect()
{
    if (readers.load() == 0)
        retired.clear();
}

bool PreviewAudio::publish (std::unique_ptr<Clip> clip, std::uint64_t expectedEpoch)
{
    if (expectedEpoch == std::numeric_limits<std::uint64_t>::max()) expectedEpoch = getCancellationEpoch();
    if (expectedEpoch != getCancellationEpoch()) return false;
    if (clip == nullptr && owned == nullptr) return true;
    const auto odd = transition.fetch_add (1) + 1;
    if (owned != nullptr)
    {
        if (previousOwned != nullptr)
            retired.push_back (std::move (previousOwned));
        previousOwned = std::move (owned);
    }
    // stop のフェード中に次の音が来た場合は、そのまま古い音からクロスフェードする。
    owned = std::move (clip);
    previous.store (previousOwned.get());
    current.store (owned.get());
    transition.store (odd + 1);
    collect();
    if (owned == nullptr) return true;
    auto state = cancelState.load();
    while ((state >> 1) == expectedEpoch)
    {
        if (cancelState.compare_exchange_weak (state, state & ~std::uint64_t (1))) return true;
    }
    return false;
}

void PreviewAudio::stop()
{
    publish (nullptr);
}

std::uint64_t PreviewAudio::addRenderer() noexcept
{
    renderers.fetch_add (1);
    const auto id = nextRendererId.fetch_add (1);
    for (auto& slot : slots)
    {
        std::uint64_t free = 0;
        if (slot.id.compare_exchange_strong (free, id))
        {
            // 絞っている最中に増えた renderer は、次の setEligibleRenderers まで足さない側に置く。
            slot.eligible.store (! narrowed.load());
            break;
        }
    }
    return id;
}

void PreviewAudio::removeRenderer (std::uint64_t id) noexcept
{
    auto expected = id;
    activeRendererId.compare_exchange_strong (expected, 0);
    for (auto& slot : slots)
    {
        auto mine = id;
        if (slot.id.compare_exchange_strong (mine, 0)) break;
    }
    renderers.fetch_sub (1);
}

const PreviewAudio::RendererSlot* PreviewAudio::findSlot (std::uint64_t id) const noexcept
{
    for (const auto& slot : slots)
        if (slot.id.load() == id) return &slot;
    return nullptr;
}

bool PreviewAudio::isEligible (std::uint64_t id) const noexcept
{
    if (! narrowed.load()) return true;
    const auto* slot = findSlot (id);
    return slot == nullptr || slot->eligible.load(); // 枠が尽きた renderer は絞らない（足せなくならないように）
}

void PreviewAudio::setEligibleRenderers (const std::vector<std::uint64_t>& ids, std::uint32_t nowMs) noexcept
{
    if (ids.empty())
    {
        narrowed.store (false);
        for (auto& slot : slots) slot.eligible.store (true);
        return;
    }
    // 持つ側がこれから呼ばれるのを待つ猶予を数える（持つ側が一度も来ないまま持たない側が足してしまわないように）。
    eligibleStampMs.store (nowMs);
    for (auto& slot : slots)
    {
        const auto id = slot.id.load();
        slot.eligible.store (id != 0 && std::find (ids.begin(), ids.end(), id) != ids.end());
    }
    narrowed.store (true);
    auto owner = activeRendererId.load();
    if (owner != 0 && ! isEligible (owner))
        activeRendererId.compare_exchange_strong (owner, 0);
}

bool PreviewAudio::renderForRenderer (juce::AudioBuffer<float>& output, double outputRate, Cursor& cursor,
                                      std::uint64_t rendererId, std::uint32_t nowMs, RenderStats* stats) noexcept
{
    if (rendererId == 0) return false;
    const auto eligible = isEligible (rendererId);
    if (narrowed.load())
    {
        // 試聴するノートの修飾を持つ renderer が動いている間は、持たない renderer（ミュートのトラックなど）は足さない。
        if (eligible) eligibleStampMs.store (nowMs);
        else if ((std::uint32_t) (nowMs - eligibleStampMs.load()) <= 250) return false;
    }
    if (renderGate.test_and_set (std::memory_order_acquire)) return false;
    auto owner = activeRendererId.load();
    if (owner != rendererId)
    {
        const auto ownerAlive = owner != 0 && (std::uint32_t) (nowMs - ownerStampMs.load()) <= 250;
        // 持たない renderer が所有している間でも、持つ renderer は代わって足す。
        const auto displace = ownerAlive && eligible && narrowed.load() && ! isEligible (owner);
        if (ownerAlive && ! displace)
        {
            renderGate.clear (std::memory_order_release);
            return false;
        }
        if (! activeRendererId.compare_exchange_strong (owner, rendererId))
        {
            renderGate.clear (std::memory_order_release);
            return false;
        }
        cursor.released = true;
    }
    ownerStampMs.store (nowMs);
    render (output, outputRate, cursor, stats);
    renderGate.clear (std::memory_order_release);
    return true;
}

void PreviewAudio::render (juce::AudioBuffer<float>& output, double outputRate, Cursor& cursor, RenderStats* stats) noexcept
{
    if (stats != nullptr) stats->frames = output.getNumSamples();
    if ((cancelState.load() & 1) != 0) { cursor.released = true; return; }
    readers.fetch_add (1);
    const Clip* clip = nullptr;
    const Clip* old = nullptr;
    std::uint64_t version = 0;
    for (int attempt = 0; attempt < 2; ++attempt)
    {
        const auto before = transition.load();
        if (before & 1) continue;
        clip = current.load();
        old = previous.load();
        if (transition.load() == before) { version = before; break; }
    }
    if (version != 0 && outputRate > 0)
    {
        if (cursor.transition != version)
        {
            if (cursor.released) old = nullptr;
            cursor.transition = version;
            cursor.fadeFrame = 0;
            cursor.released = false;
        }
        else if (cursor.released) old = nullptr;
        if (stats != nullptr)
        {
            stats->transition = version;
            stats->active = clip != nullptr;
            stats->release = clip == nullptr && old != nullptr;
        }
        const auto channels = output.getNumChannels();
        const auto fadeFrames = juce::jmax (1, (int) (outputRate * 0.006));
        for (int i = 0; i < output.getNumSamples(); ++i)
        {
            const auto gain = juce::jlimit (0.0f, 1.0f, (float) cursor.fadeFrame / (float) fadeFrames);
            bool nonZero = false;
            for (int ch = 0; ch < channels; ++ch)
            {
                const auto sample = [] (const Clip* c, int channel, double sec) noexcept -> float
                {
                    if (c == nullptr || c->frames() < 2 || c->sampleRate <= 0) return 0.0f;
                    const auto frame = std::fmod (sec * c->sampleRate, (double) c->frames());
                    const auto a = (int) frame;
                    const auto b = a + 1 < c->frames() ? a + 1 : 0;
                    const auto& pcm = c->channels[(size_t) juce::jmin (channel, (int) c->channels.size() - 1)];
                    return pcm[(size_t) a] + (pcm[(size_t) b] - pcm[(size_t) a]) * (float) (frame - a);
                };
                const auto added = sample (old, ch, cursor.phaseSec) * (1.0f - gain)
                                 + sample (clip, ch, cursor.phaseSec) * gain;
                output.getWritePointer (ch)[i] += added;
                if (stats != nullptr)
                {
                    nonZero |= added != 0.0f;
                    stats->energy += (double) added * added;
                }
            }
            if (stats != nullptr && nonZero) ++stats->nonZeroFrames;
            cursor.phaseSec += 1.0 / outputRate;
            if (cursor.fadeFrame < fadeFrames) ++cursor.fadeFrame;
        }
        if (clip == nullptr && cursor.fadeFrame >= fadeFrames)
        {
            cursor.phaseSec = 0.0;
            cursor.released = true;
        }
    }
    readers.fetch_sub (1);
}

} // namespace gliss
