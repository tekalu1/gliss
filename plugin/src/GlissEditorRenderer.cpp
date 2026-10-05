#include "GlissEditorRenderer.h"
#include "Diagnostics.h"

namespace gliss
{

GlissEditorRenderer::GlissEditorRenderer (ARA::PlugIn::DocumentController* controller, std::shared_ptr<PreviewAudio> preview)
    : ARAEditorRenderer (controller), previewAudio (std::move (preview))
{
    rendererId = previewAudio->addRenderer();
}

GlissEditorRenderer::~GlissEditorRenderer()
{
    if (prepared) dumpTrace();
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
    tracing = diag::traceEnabled();
    traceCount = 0;
    if (tracing) trace.assign (1 << 16, {});
    prepared = true;
}

void GlissEditorRenderer::releaseResources()
{
    if (prepared) dumpTrace();
    trace.clear();
    trace.shrink_to_fit();
    prepared = false;
    ARAEditorRenderer::releaseResources();
}

bool GlissEditorRenderer::processBlock (juce::AudioBuffer<float>& buffer,
                                        juce::AudioProcessor::Realtime realtime,
                                        const juce::AudioPlayHead::PositionInfo& position) noexcept
{
    PreviewAudio::RenderStats stats;
    char mode = 'r';
    if (position.getIsPlaying())
    {
        if (! hostPlaying) previewAudio->cancelFromAudioThread();
        hostPlaying = true;
        cursor.released = true;
        mode = 'h';
    }
    else if (realtime == juce::AudioProcessor::Realtime::yes)
    {
        hostPlaying = false;
        if (! previewAudio->renderForRenderer (buffer, outputRate, cursor, rendererId,
                                               juce::Time::getMillisecondCounter(), tracing ? &stats : nullptr))
            mode = 'x';
    }
    else
    {
        hostPlaying = false;
        cursor.released = true;
        mode = 'o';
    }
    if (tracing)
    {
        const auto index = traceCount.fetch_add (1, std::memory_order_relaxed);
        auto& entry = trace[index % trace.size()];
        entry = { index, buffer.getNumSamples(), stats.nonZeroFrames, stats.energy,
                  stats.transition, stats.active, stats.release, mode };
    }
    return true;
}

void GlissEditorRenderer::dumpTrace()
{
    if (! tracing) return;
    const auto count = traceCount.load();
    const auto first = count > trace.size() ? count - trace.size() : 0;
    size_t frames = 0, nonZero = 0, stopZeroBlocks = 0;
    double energy = 0.0;
    bool sawRelease = false;
    for (size_t i = first; i < count; ++i)
    {
        const auto& t = trace[i % trace.size()];
        if (t.block != i) continue;
        if (t.mode == 'r') frames += (size_t) t.frames;
        nonZero += (size_t) t.nonZeroFrames;
        energy += t.energy;
        if (t.release) sawRelease = true;
        if (sawRelease && t.mode == 'r' && ! t.active && ! t.release && t.nonZeroFrames == 0)
            ++stopZeroBlocks;
        diag::log ("preview-trace renderer=" + juce::String ((juce::int64) rendererId)
                   + " block=" + juce::String ((juce::int64) i) + " mode=" + juce::String::charToString (t.mode)
                   + " frames=" + juce::String (t.frames) + " nonzero=" + juce::String (t.nonZeroFrames)
                   + " energy=" + juce::String (t.energy, 9) + " transition=" + juce::String ((juce::int64) t.transition)
                   + " active=" + juce::String (t.active ? 1 : 0) + " release=" + juce::String (t.release ? 1 : 0));
    }
    diag::log ("preview: release renderer=" + juce::String ((juce::int64) rendererId)
               + " blocks=" + juce::String ((juce::int64) count) + " logged=" + juce::String ((juce::int64) (count - first))
               + " frames=" + juce::String ((juce::int64) frames) + " nonzero=" + juce::String ((juce::int64) nonZero)
               + " energy=" + juce::String (energy, 9)
               + " stopZeroBlocks=" + juce::String ((juce::int64) stopZeroBlocks));
}

} // namespace gliss
