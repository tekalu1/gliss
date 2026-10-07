#include "GlissEditorRenderer.h"
#include "Diagnostics.h"

namespace gliss
{

GlissEditorRenderer::GlissEditorRenderer (ARA::PlugIn::DocumentController* controller, std::shared_ptr<PreviewAudio> preview)
    : ARAEditorRenderer (controller), previewAudio (std::move (preview))
{
    rendererId = previewAudio->addRenderer();
    diag::log ("preview: editor renderer " + juce::String ((juce::int64) rendererId) + " created");
}

GlissEditorRenderer::~GlissEditorRenderer()
{
    closeTrace();
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
    closeTrace();
    if (diag::traceEnabled())
    {
        traceState = std::make_shared<TraceState> (rendererId);
        startTimer (100);
    }
}

void GlissEditorRenderer::releaseResources()
{
    closeTrace();
    ARAEditorRenderer::releaseResources();
}

bool GlissEditorRenderer::coversModification (const juce::String& araId) const
{
    const auto isIt = [&araId] (const juce::ARAPlaybackRegion* region)
    {
        const auto* modification = region != nullptr ? region->getAudioModification() : nullptr;
        return modification != nullptr && juce::String (modification->getPersistentID()) == araId;
    };

    for (const auto* region : getPlaybackRegions<juce::ARAPlaybackRegion>())
        if (isIt (region)) return true;

    for (const auto* sequence : getRegionSequences<juce::ARARegionSequence>())
        for (const auto* region : sequence->getPlaybackRegions<juce::ARAPlaybackRegion>())
            if (isIt (region)) return true;

    return false;
}

void GlissEditorRenderer::closeTrace()
{
    stopTimer();
    auto state = std::move (traceState);
    if (state == nullptr) return;
    const auto flush = [state] { state->drain(); state->finish(); };
    auto* messages = juce::MessageManager::getInstanceWithoutCreating();
    if (messages != nullptr && messages->isThisTheMessageThread()) flush();
    else if (messages != nullptr) juce::MessageManager::callAsync (flush);
}

void GlissEditorRenderer::timerCallback()
{
    if (traceState != nullptr) traceState->drain();
}

bool GlissEditorRenderer::processBlock (juce::AudioBuffer<float>& buffer,
                                        juce::AudioProcessor::Realtime realtime,
                                        const juce::AudioPlayHead::PositionInfo& position) noexcept
{
    PreviewAudio::RenderStats stats;
    char mode = 'r';
    std::uint32_t nowMs = 0;
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
        nowMs = juce::Time::getMillisecondCounter();
        if (! previewAudio->renderForRenderer (buffer, outputRate, cursor, rendererId,
                                               nowMs, traceState != nullptr ? &stats : nullptr))
            mode = 'x';
    }
    else
    {
        hostPlaying = false;
        cursor.released = true;
        mode = 'o';
    }
    if (auto* state = traceState.get())
    {
        if (mode == 'h' || mode == 'o') nowMs = juce::Time::getMillisecondCounter();
        const auto write = state->written.load (std::memory_order_relaxed);
        if (write - state->read.load (std::memory_order_acquire) < state->entries.size())
        {
            state->entries[write % state->entries.size()] =
                { write, nowMs, buffer.getNumSamples(), stats.nonZeroFrames, stats.energy,
                  stats.transition, stats.active, stats.release, mode };
            state->written.store (write + 1, std::memory_order_release);
        }
        else state->dropped.fetch_add (1, std::memory_order_relaxed);
    }
    return true;
}

void GlissEditorRenderer::TraceState::drain()
{
    const auto end = written.load (std::memory_order_acquire);
    auto first = read.load (std::memory_order_relaxed);
    for (auto i = first; i < end; ++i)
    {
        const auto& t = entries[i % entries.size()];
        if (t.mode == 'r') frames += (size_t) t.frames;
        nonZero += (size_t) t.nonZeroFrames;
        energy += t.energy;
        if (t.release) sawRelease = true;
        if (sawRelease && t.mode == 'r' && ! t.active && ! t.release && t.nonZeroFrames == 0)
            ++stopZeroBlocks;
        diag::log ("preview-trace renderer=" + juce::String ((juce::int64) rendererId)
                   + " block=" + juce::String ((juce::int64) i) + " ms=" + juce::String (t.ms)
                   + " mode=" + juce::String::charToString (t.mode)
                   + " frames=" + juce::String (t.frames) + " nonzero=" + juce::String (t.nonZeroFrames)
                   + " energy=" + juce::String (t.energy, 9) + " transition=" + juce::String ((juce::int64) t.transition)
                   + " active=" + juce::String (t.active ? 1 : 0) + " release=" + juce::String (t.release ? 1 : 0));
    }
    read.store (end, std::memory_order_release);
}

void GlissEditorRenderer::TraceState::finish()
{
    diag::log ("preview: release renderer=" + juce::String ((juce::int64) rendererId)
               + " blocks=" + juce::String ((juce::int64) written.load())
               + " dropped=" + juce::String ((juce::int64) dropped.load())
               + " frames=" + juce::String ((juce::int64) frames) + " nonzero=" + juce::String ((juce::int64) nonZero)
               + " energy=" + juce::String (energy, 9)
               + " stopZeroBlocks=" + juce::String ((juce::int64) stopZeroBlocks));
}

} // namespace gliss
