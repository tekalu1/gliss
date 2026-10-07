#pragma once

#include <juce_audio_basics/juce_audio_basics.h>
#include <array>
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
    struct RenderStats
    {
        int frames = 0, nonZeroFrames = 0;
        double energy = 0.0;
        std::uint64_t transition = 0;
        bool active = false, release = false;
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
    void render (juce::AudioBuffer<float>& output, double outputRate, Cursor& cursor, RenderStats* stats = nullptr) noexcept;
    std::uint64_t addRenderer() noexcept;
    void removeRenderer (std::uint64_t id) noexcept;
    bool hasRenderer() const noexcept { return renderers.load() > 0; }
    /** 試聴するノートの修飾を持つ EditorRenderer の id を渡す（メッセージスレッド）。ここに入った renderer だけが試聴を足す。
        空なら（修飾を持つ renderer が無い・ホストが領域を渡さない）絞らず、どの renderer も足せる。
        所有者が絞りから外れたら外す。持つ renderer が 250 ms 呼ばれなければ、持たない renderer が足す（持つ側が止まっている）。 */
    void setEligibleRenderers (const std::vector<std::uint64_t>& ids, std::uint32_t nowMs) noexcept;

    /** 足してよい renderer の絞り込みの規則: 修飾を持つ renderer（covering）があればそれ、無ければ試聴を求めた側（requester。登録されていなければ無いものとする）、
        それも無ければ絞らない（空）。 */
    std::vector<std::uint64_t> chooseEligible (const std::vector<std::uint64_t>& covering, std::uint64_t requester) const;

    /** 試聴を始めるたびに呼ぶ（メッセージスレッド）。chooseEligible で絞り、前の試聴の所有者を外す（前の所有者が、絞れないまま足し続けない）。 */
    void beginPreview (const std::vector<std::uint64_t>& covering, std::uint64_t requester, std::uint32_t nowMs) noexcept;

    /** 試聴の様子（ログ用。オーディオスレッドが書く値をメッセージスレッドが読む）。 */
    struct PreviewStats
    {
        std::uint64_t owner = 0;        // 今の所有者（外れていれば 0）
        std::uint64_t playedBy = 0;     // 最後に足した renderer
        bool narrowed = false;
        int handovers = 0;              // beginPreview の後、足す renderer が別の renderer に移った回数
    };
    PreviewStats getStats() const noexcept;
    bool renderForRenderer (juce::AudioBuffer<float>& output, double outputRate, Cursor& cursor,
                            std::uint64_t rendererId, std::uint32_t nowMs, RenderStats* stats = nullptr) noexcept;

private:
    /** 登録した renderer の枠。オーディオスレッドが id から eligible を引く（ロックしない）。 */
    struct RendererSlot
    {
        std::atomic<std::uint64_t> id { 0 };
        std::atomic<bool> eligible { true };
    };
    static constexpr int maxRendererSlots = 128;

    const RendererSlot* findSlot (std::uint64_t id) const noexcept;
    bool isEligible (std::uint64_t id) const noexcept;
    void collect();
    std::atomic<const Clip*> current { nullptr }, previous { nullptr };
    std::atomic<std::uint64_t> transition { 0 };
    std::atomic<std::uint64_t> cancelState { 0 }; // upper bits: generation, low bit: host playback suppression
    std::atomic<int> readers { 0 }, renderers { 0 };
    std::atomic<std::uint64_t> nextRendererId { 1 }, activeRendererId { 0 };
    std::atomic<std::uint32_t> ownerStampMs { 0 }, eligibleStampMs { 0 };
    std::atomic<bool> narrowed { false };   // setEligibleRenderers が renderer を絞っている
    std::atomic<std::uint64_t> lastPlayedBy { 0 };
    std::atomic<int> handoverCount { 0 };
    std::array<RendererSlot, maxRendererSlots> slots;
    std::atomic_flag renderGate = ATOMIC_FLAG_INIT;
    std::unique_ptr<Clip> owned;
    std::unique_ptr<Clip> previousOwned;
    std::vector<std::unique_ptr<Clip>> retired;
};

} // namespace gliss
