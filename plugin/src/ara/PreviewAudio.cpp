#include "PreviewAudio.h"
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

void PreviewAudio::removeRenderer (std::uint64_t id) noexcept
{
    auto expected = id;
    activeRendererId.compare_exchange_strong (expected, 0);
    renderers.fetch_sub (1);
}

bool PreviewAudio::renderForRenderer (juce::AudioBuffer<float>& output, double outputRate, Cursor& cursor,
                                      std::uint64_t rendererId, std::uint32_t nowMs) noexcept
{
    if (rendererId == 0 || renderGate.test_and_set (std::memory_order_acquire)) return false;
    auto owner = activeRendererId.load();
    if (owner != rendererId)
    {
        if (owner != 0 && (std::uint32_t) (nowMs - ownerStampMs.load()) <= 250)
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
    render (output, outputRate, cursor);
    renderGate.clear (std::memory_order_release);
    return true;
}

void PreviewAudio::render (juce::AudioBuffer<float>& output, double outputRate, Cursor& cursor) noexcept
{
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
        const auto channels = output.getNumChannels();
        const auto fadeFrames = juce::jmax (1, (int) (outputRate * 0.006));
        for (int i = 0; i < output.getNumSamples(); ++i)
        {
            const auto gain = juce::jlimit (0.0f, 1.0f, (float) cursor.fadeFrame / (float) fadeFrames);
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
                output.getWritePointer (ch)[i] += sample (old, ch, cursor.phaseSec) * (1.0f - gain)
                                               + sample (clip, ch, cursor.phaseSec) * gain;
            }
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
