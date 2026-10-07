#pragma once

#include <juce_audio_processors/juce_audio_processors.h>
#include "ara/PreviewAudio.h"
#include <atomic>
#include <memory>
#include <vector>

namespace gliss
{

/** 補正後の試聴 PCM を DAW の出力に足す。通常再生中とオフライン描画では足さない。 */
class GlissEditorRenderer final : public juce::ARAEditorRenderer, private juce::Timer
{
public:
    GlissEditorRenderer (ARA::PlugIn::DocumentController* controller, std::shared_ptr<PreviewAudio> preview);
    ~GlissEditorRenderer() override;

    void prepareToPlay (double sampleRate, int maximumSamplesPerBlock, int numChannels,
                        juce::AudioProcessor::ProcessingPrecision precision,
                        AlwaysNonRealtime alwaysNonRealtime = AlwaysNonRealtime::no) override;
    void releaseResources() override;
    void reset() override { cursor = {}; hostPlaying = false; }

    bool processBlock (juce::AudioBuffer<float>& buffer,
                       juce::AudioProcessor::Realtime realtime,
                       const juce::AudioPlayHead::PositionInfo& positionInfo) noexcept override;

    using ARAEditorRenderer::processBlock;

    std::uint64_t getRendererId() const noexcept { return rendererId; }
    /** DAW がこの renderer に割り当てた再生リージョン（またはリージョン列）に、この修飾（persistentID）のリージョンがあるか。
        ARA のメインスレッド（メッセージスレッド）から呼ぶ。試聴を足す renderer を選ぶのに使う。 */
    bool coversModification (const juce::String& araId) const;

private:
    struct TraceEntry
    {
        size_t block = 0;
        std::uint32_t ms = 0;
        int frames = 0, nonZeroFrames = 0;
        double energy = 0.0;
        std::uint64_t transition = 0;
        bool active = false, release = false;
        char mode = 'r'; // r: renderer, h: host playback, o: offline, x: another renderer owns the preview
    };
    struct TraceState
    {
        explicit TraceState (std::uint64_t id) : rendererId (id), entries (8192) {}
        void drain(); // message thread only
        void finish(); // message thread only
        std::uint64_t rendererId;
        std::vector<TraceEntry> entries;
        std::atomic<size_t> written { 0 }, read { 0 }, dropped { 0 };
        size_t frames = 0, nonZero = 0, stopZeroBlocks = 0;
        double energy = 0.0;
        bool sawRelease = false;
    };
    void timerCallback() override;
    void closeTrace();
    std::shared_ptr<PreviewAudio> previewAudio;
    double outputRate = 0.0;
    PreviewAudio::Cursor cursor;
    bool hostPlaying = false;
    std::uint64_t rendererId = 0;
    std::shared_ptr<TraceState> traceState;
};

} // namespace gliss
