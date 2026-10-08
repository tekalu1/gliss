#pragma once

#include <juce_audio_processors/juce_audio_processors.h>
#include <memory>

namespace gliss
{

/** 同じプラグインのインスタンスの PlaybackRenderer と EditorRenderer を結ぶ（GlissProcessor::didBindToARA が作って両方に渡す）。
    DAW によっては EditorRenderer にリージョンを割り当てない（Studio Pro）が、PlaybackRenderer には再生のために必ず割り当てる（ARA の規則）。
    試聴を足す EditorRenderer を絞るとき、EditorRenderer の割り当てが空なら、同じインスタンスの PlaybackRenderer の割り当てを手掛かりにする。
    メッセージスレッド（ARA のメインスレッド）だけが触る。PlaybackRenderer が壊れるときに playback を外す。 */
struct RendererPair
{
    const juce::ARAPlaybackRenderer* playback = nullptr;
};

/** regions の中に、この修飾（persistentID）のリージョンがあるか。 */
template <typename Regions>
bool hasRegionOfModification (const Regions& regions, const juce::String& araId)
{
    for (const auto* region : regions)
    {
        const auto* modification = region != nullptr ? region->getAudioModification() : nullptr;

        if (modification != nullptr && juce::String (modification->getPersistentID()) == araId)
            return true;
    }

    return false;
}

} // namespace gliss
