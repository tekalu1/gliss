#pragma once

#include <juce_core/juce_core.h>
#include <cstdint>

namespace gliss
{

/** 画面の preview の引数に、試聴を求めたエディタのインスタンスの EditorRenderer の id（requester）を添える（画面のコードは変えない）。
    プラグインは、試聴を足す EditorRenderer を絞るのに使う（PreviewAudio::chooseEligible）。arg がオブジェクトでなければ、0 のときはそのまま返す。 */
inline juce::var withRequester (const juce::var& arg, std::uint64_t rendererId)
{
    if (rendererId == 0 || ! arg.isObject())
        return arg;

    auto copy = arg.clone();
    copy.getDynamicObject()->setProperty ("requester", (juce::int64) rendererId);
    return copy;
}

} // namespace gliss
