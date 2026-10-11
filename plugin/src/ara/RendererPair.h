#pragma once

#include <juce_audio_processors/juce_audio_processors.h>
#include <algorithm>
#include <memory>
#include <vector>

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

/** regions の中で、この修飾のリージョンが載っているトラック（リージョン列）を探した結果。 */
struct SequenceMatch
{
    bool found = false;
    const juce::ARARegionSequence* sequence = nullptr;
};

/** regions の中から、この修飾のリージョンで、only のトラックに載っているものを探す（only が空ならどのトラックでもよい）。
    試聴を足すインスタンスを、DAW で選んだトラックのものに絞るのと、ログにトラック名を書くのに使う。 */
template <typename Regions>
SequenceMatch findSequenceOfModification (const Regions& regions, const juce::String& araId,
                                          const std::vector<const juce::ARARegionSequence*>& only)
{
    for (const auto* region : regions)
    {
        const auto* modification = region != nullptr ? region->getAudioModification() : nullptr;

        if (modification == nullptr || juce::String (modification->getPersistentID()) != araId)
            continue;

        const auto* sequence = region->template getRegionSequence<juce::ARARegionSequence>();

        if (only.empty() || std::find (only.begin(), only.end(), sequence) != only.end())
            return { true, sequence };
    }

    return {};
}

} // namespace gliss
