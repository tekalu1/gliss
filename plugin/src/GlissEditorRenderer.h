#pragma once

#include <juce_audio_processors/juce_audio_processors.h>
#include "ara/PreviewAudio.h"
#include <atomic>
#include <memory>
#include <vector>

namespace gliss
{

/** 補正後の試聴 PCM を DAW の出力に足す。通常再生中とオフライン描画では足さない。 */
class GlissEditorRenderer final : public juce::ARAEditorRenderer
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
private:
    struct TraceEntry
    {
        size_t block = 0;
        int frames = 0, nonZeroFrames = 0;
        double energy = 0.0;
        std::uint64_t transition = 0;
        bool active = false, release = false;
        char mode = 'r'; // r: renderer, h: host playback, o: offline, x: another renderer owns the preview
    };
    void dumpTrace();
    std::shared_ptr<PreviewAudio> previewAudio;
    double outputRate = 0.0;
    PreviewAudio::Cursor cursor;
    bool hostPlaying = false;
    std::uint64_t rendererId = 0;
    bool prepared = false, tracing = false;
    std::vector<TraceEntry> trace;
    std::atomic<size_t> traceCount { 0 };
};

} // namespace gliss
