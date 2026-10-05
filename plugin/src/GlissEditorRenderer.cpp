#include "GlissEditorRenderer.h"

namespace gliss
{

GlissEditorRenderer::GlissEditorRenderer (ARA::PlugIn::DocumentController* controller, std::shared_ptr<PreviewAudio> preview)
    : ARAEditorRenderer (controller), previewAudio (std::move (preview))
{
    rendererId = previewAudio->addRenderer();
}

GlissEditorRenderer::~GlissEditorRenderer()
{
    previewAudio->removeRenderer (rendererId);
}

void GlissEditorRenderer::prepareToPlay (double sampleRate, int maximumSamplesPerBlock, int numChannels,
                                         juce::AudioProcessor::ProcessingPrecision precision,
                                         AlwaysNonRealtime alwaysNonRealtime)
{
    ARAEditorRenderer::prepareToPlay (sampleRate, maximumSamplesPerBlock, numChannels, precision, alwaysNonRealtime);
    outputRate = sampleRate;
    cursor = {};
    hostPlaying = false;
}

bool GlissEditorRenderer::processBlock (juce::AudioBuffer<float>& buffer,
                                        juce::AudioProcessor::Realtime realtime,
                                        const juce::AudioPlayHead::PositionInfo& position) noexcept
{
    if (position.getIsPlaying())
    {
        if (! hostPlaying) previewAudio->cancelFromAudioThread();
        hostPlaying = true;
        cursor.released = true;
    }
    else if (realtime == juce::AudioProcessor::Realtime::yes)
    {
        hostPlaying = false;
        previewAudio->renderForRenderer (buffer, outputRate, cursor, rendererId, juce::Time::getMillisecondCounter());
    }
    else
    {
        hostPlaying = false;
        cursor.released = true;
    }
    return true;
}

} // namespace gliss
