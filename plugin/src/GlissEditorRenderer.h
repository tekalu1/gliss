#pragma once

#include <juce_audio_processors/juce_audio_processors.h>

namespace gliss
{

/** EditorRenderer（編集中の試聴を DAW の出力に足す役）。つかんだノートの試聴（render_audition の WAV）はまだ作っていないので、
    何も足さない（DocumentBridge::preview は {ok:false}、bootstrap の preview は false）。

    作るときの注意（research-products-ux §5.3）: Reaper は PlaybackRenderer と EditorRenderer を同じバッファで順に呼ぶ。
    再生中に鳴らすと二重に鳴るので、止まっているときだけ鳴らす。 */
class GlissEditorRenderer final : public juce::ARAEditorRenderer
{
public:
    using ARAEditorRenderer::ARAEditorRenderer;

    bool processBlock (juce::AudioBuffer<float>& buffer,
                       juce::AudioProcessor::Realtime realtime,
                       const juce::AudioPlayHead::PositionInfo& positionInfo) noexcept override;

    using ARAEditorRenderer::processBlock;
};

} // namespace gliss
