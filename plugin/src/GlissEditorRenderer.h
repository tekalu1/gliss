#pragma once

#include <juce_audio_processors/juce_audio_processors.h>
#include "ara/PreviewAudio.h"
#include <memory>

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
    void reset() override { cursor = {}; hostPlaying = false; }

    bool processBlock (juce::AudioBuffer<float>& buffer,
                       juce::AudioProcessor::Realtime realtime,
                       const juce::AudioPlayHead::PositionInfo& positionInfo) noexcept override;

    using ARAEditorRenderer::processBlock;
private:
    std::shared_ptr<PreviewAudio> previewAudio;
    double outputRate = 0.0;
    PreviewAudio::Cursor cursor;
    bool hostPlaying = false;
};

} // namespace gliss
