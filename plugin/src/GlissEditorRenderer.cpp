#include "GlissEditorRenderer.h"

namespace gliss
{

bool GlissEditorRenderer::processBlock (juce::AudioBuffer<float>&,
                                        juce::AudioProcessor::Realtime,
                                        const juce::AudioPlayHead::PositionInfo&) noexcept
{
    return true;   // 何も足さない（PlaybackRenderer の出力をそのまま通す）
}

} // namespace gliss
