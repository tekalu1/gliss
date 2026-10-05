#pragma once

#include <juce_audio_basics/juce_audio_basics.h>
#include <atomic>
#include <cstdint>
#include <limits>
#include <memory>
#include <vector>

namespace gliss
{

/** メッセージ側で準備した短い PCM を EditorRenderer に渡す。render はロック・確保・IO を行わない。 */
class PreviewAudio
{
public:
    struct Clip
    {
        double sampleRate = 0.0;
        std::vector<std::vector<float>> channels;
        int frames() const noexcept { return channels.empty() ? 0 : (int) channels[0].size(); }
    };
    struct Cursor
    {
        double phaseSec = 0.0;
        std::uint64_t transition = 0;
        int fadeFrame = 0;
        bool released = false;
    };

    bool publish (std::unique_ptr<Clip> clip, std::uint64_t expectedEpoch = std::numeric_limits<std::uint64_t>::max());
    static void fadeEdges (Clip& clip) noexcept;
    void stop();
    void cancelFromAudioThread() noexcept
    {
        auto old = cancelState.load();
        while (! cancelState.compare_exchange_weak (old, ((old >> 1) + 1) * 2 + 1)) {}
        activeRendererId.store (0);
    }
    std::uint64_t getCancellationEpoch() const noexcept { return cancelState.load() >> 1; }
    void render (juce::AudioBuffer<float>& output, double outputRate, Cursor& cursor) noexcept;
    std::uint64_t addRenderer() noexcept { renderers.fetch_add (1); return nextRendererId.fetch_add (1); }
    void removeRenderer (std::uint64_t id) noexcept;
    bool hasRenderer() const noexcept { return renderers.load() > 0; }
    bool renderForRenderer (juce::AudioBuffer<float>& output, double outputRate, Cursor& cursor,
                            std::uint64_t rendererId, std::uint32_t nowMs) noexcept;

private:
    void collect();
    std::atomic<const Clip*> current { nullptr }, previous { nullptr };
    std::atomic<std::uint64_t> transition { 0 };
    std::atomic<std::uint64_t> cancelState { 0 }; // upper bits: generation, low bit: host playback suppression
    std::atomic<int> readers { 0 }, renderers { 0 };
    std::atomic<std::uint64_t> nextRendererId { 1 }, activeRendererId { 0 };
    std::atomic<std::uint32_t> ownerStampMs { 0 };
    std::atomic_flag renderGate = ATOMIC_FLAG_INIT;
    std::unique_ptr<Clip> owned;
    std::unique_ptr<Clip> previousOwned;
    std::vector<std::unique_ptr<Clip>> retired;
};

} // namespace gliss
