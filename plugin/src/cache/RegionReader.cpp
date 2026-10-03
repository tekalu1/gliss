#include "RegionReader.h"

#include <algorithm>
#include <array>
#include <cmath>

namespace gliss
{

//==============================================================================
ResampleState::ResampleState() = default;

void ResampleState::reset() noexcept
{
    nextExpectedSourceSample = -1;
    for (auto& interp : interpolators)
        interp.reset();
}

juce::int64 ResampleState::checkContinuityAndGetStart (juce::int64 startInSource) noexcept
{
    if (nextExpectedSourceSample < 0 || std::abs (startInSource - nextExpectedSourceSample) > 1)
    {
        reset();
        nextExpectedSourceSample = startInSource;
        return startInSource;
    }

    return nextExpectedSourceSample;
}

void ResampleState::advanceSourceSamples (int numUsedSourceSamples) noexcept
{
    if (nextExpectedSourceSample >= 0)
        nextExpectedSourceSample += numUsedSourceSamples;
}

juce::WindowedSincInterpolator& ResampleState::getInterpolator (int channel) noexcept
{
    if ((size_t) channel >= interpolators.size())
        interpolators.resize ((size_t) channel + 1);

    return interpolators[(size_t) channel];
}

//==============================================================================
namespace
{

void writeToDest (juce::AudioBuffer<float>& dest,
                  int destStart,
                  int numSamples,
                  const juce::AudioBuffer<float>& src,
                  bool add) noexcept
{
    const auto srcChans = src.getNumChannels();
    const auto destChans = dest.getNumChannels();

    if (srcChans == 1 && destChans >= 2)
    {
        // モノラル -> ステレオ以上（全出力チャンネルに複製）
        const auto* s = src.getReadPointer (0);
        for (int ch = 0; ch < destChans; ++ch)
        {
            if (add)
                dest.addFrom (ch, destStart, s, numSamples);
            else
                dest.copyFrom (ch, destStart, s, numSamples);
        }
    }
    else if (srcChans == 2 && destChans == 1)
    {
        // ステレオ -> モノラル（平均）
        const auto* l = src.getReadPointer (0);
        const auto* r = src.getReadPointer (1);
        auto* d = dest.getWritePointer (0, destStart);
        for (int i = 0; i < numSamples; ++i)
        {
            const float val = 0.5f * (l[i] + r[i]);
            if (add)
                d[i] += val;
            else
                d[i] = val;
        }
    }
    else
    {
        // 1:1 またはその他のチャンネルマッピング
        for (int ch = 0; ch < destChans; ++ch)
        {
            const auto srcCh = std::min (ch, srcChans - 1);
            const auto* s = src.getReadPointer (srcCh);
            if (add)
                dest.addFrom (ch, destStart, s, numSamples);
            else
                dest.copyFrom (ch, destStart, s, numSamples);
        }
    }
}

void applyWindowsToBuffer (juce::AudioBuffer<float>& buffer,
                           juce::int64 startInSource,
                           int numSamples,
                           const EditedPcmSnapshot& snapshot) noexcept
{
    std::vector<const EditedWindow*> overlapping;
    snapshot.getOverlappingWindows (startInSource, numSamples, overlapping);

    const auto bufferChannels = buffer.getNumChannels();

    for (const auto* win : overlapping)
    {
        const auto overlapStart = std::max (startInSource, win->startFrame);
        const auto overlapEnd = std::min (startInSource + (juce::int64) numSamples, win->getEndFrame());

        if (overlapStart < overlapEnd)
        {
            const auto count = (int) (overlapEnd - overlapStart);
            const auto destOffset = (int) (overlapStart - startInSource);
            const auto winOffset = (int) (overlapStart - win->startFrame);
            const auto copyChans = std::min (bufferChannels, win->getNumChannels());

            for (int ch = 0; ch < copyChans; ++ch)
            {
                buffer.copyFrom (ch, destOffset, win->buffer, ch, winOffset, count);
            }
        }
    }
}

} // namespace

bool RegionReader::readBlock (juce::AudioBuffer<float>& destBuffer,
                             int destStartSample,
                             int numDestSamples,
                             juce::int64 startInSource,
                             double hostSampleRate,
                             SourceReader* sourceReader,
                             const EditedPcm* editedPcm,
                             ResampleState* resampleState,
                             const RegionReadOptions& options) noexcept
{
    if (numDestSamples <= 0)
        return true;

    // ソースのフォーマットを取得
    double sourceSampleRate = hostSampleRate;
    int sourceChannels = destBuffer.getNumChannels();

    if (sourceReader != nullptr)
    {
        sourceSampleRate = sourceReader->getSampleRate();
        sourceChannels = sourceReader->getNumChannels();
    }
    else if (editedPcm != nullptr)
    {
        sourceSampleRate = editedPcm->getSampleRate();
        sourceChannels = editedPcm->getNumChannels();
    }

    if (sourceSampleRate <= 0.0)
        sourceSampleRate = hostSampleRate;
    if (sourceChannels <= 0)
        sourceChannels = destBuffer.getNumChannels();

    const bool sameSampleRate = std::abs (sourceSampleRate - hostSampleRate) < 1.0;

    // スナップショットを取得（tryLock）
    std::shared_ptr<const EditedPcmSnapshot> snapshot;
    if (! options.compareMode && editedPcm != nullptr)
        snapshot = editedPcm->tryGetSnapshot();

    //--------------------------------------------------------------------------
    // ケース 1: サンプリング周波数が同じ場合（素通し）
    //--------------------------------------------------------------------------
    if (sameSampleRate)
    {
        constexpr int maxStackSamples = 4096;
        constexpr int maxStackChannels = 2;
        float stackMemory[maxStackChannels * maxStackSamples];
        float* channelPointers[maxStackChannels];

        auto runWithBuffer = [&] (juce::AudioBuffer<float>& sourceBuffer)
        {
            // 1. 原音を読み出し
            bool complete = true;
            if (sourceReader != nullptr)
                complete = sourceReader->readSourceSamples (sourceBuffer, 0, numDestSamples, startInSource, options.timeoutMs);
            else
                sourceBuffer.clear();

            // 2. 窓を上書き
            if (snapshot != nullptr && snapshot->hasWindows())
                applyWindowsToBuffer (sourceBuffer, startInSource, numDestSamples, *snapshot);

            // 3. チャンネル変換して出力
            writeToDest (destBuffer, destStartSample, numDestSamples, sourceBuffer, options.addToDestBuffer);

            return complete;
        };

        if (sourceChannels <= maxStackChannels && numDestSamples <= maxStackSamples)
        {
            for (int ch = 0; ch < sourceChannels; ++ch)
                channelPointers[ch] = stackMemory + ch * numDestSamples;

            juce::AudioBuffer<float> stackBuffer (channelPointers, sourceChannels, numDestSamples);
            return runWithBuffer (stackBuffer);
        }

        juce::AudioBuffer<float> heapBuffer (sourceChannels, numDestSamples);
        return runWithBuffer (heapBuffer);
    }

    //--------------------------------------------------------------------------
    // ケース 2: サンプリング周波数が異なる場合（リサンプリング）
    //--------------------------------------------------------------------------
    const double speedRatio = sourceSampleRate / hostSampleRate;

    juce::int64 actualStartInSource = startInSource;
    if (resampleState != nullptr)
        actualStartInSource = resampleState->checkContinuityAndGetStart (startInSource);

    // 必要な入力サンプル数（マージン 64 サンプル）
    const int numSourceSamples = (int) std::ceil (numDestSamples * speedRatio) + 64;

    constexpr int maxStackSamples = 4096;
    constexpr int maxStackChannels = 2;
    float stackSourceMemory[maxStackChannels * maxStackSamples];
    float* stackSourcePointers[maxStackChannels];

    float stackResampledMemory[maxStackChannels * maxStackSamples];
    float* stackResampledPointers[maxStackChannels];

    auto runResampled = [&] (juce::AudioBuffer<float>& sourceBuffer,
                             juce::AudioBuffer<float>& resampledBuffer)
    {
        // 1. 原音を読み出し
        bool complete = true;
        if (sourceReader != nullptr)
            complete = sourceReader->readSourceSamples (sourceBuffer, 0, numSourceSamples, actualStartInSource, options.timeoutMs);
        else
            sourceBuffer.clear();

        // 2. 窓を上書き（リサンプラーに入る前に結合するので継ぎ目に段差が出ない）
        if (snapshot != nullptr && snapshot->hasWindows())
            applyWindowsToBuffer (sourceBuffer, actualStartInSource, numSourceSamples, *snapshot);

        // 3. リサンプリング
        int usedSamples = 0;
        for (int ch = 0; ch < sourceChannels; ++ch)
        {
            juce::WindowedSincInterpolator localInterp;
            auto& interp = (resampleState != nullptr ? resampleState->getInterpolator (ch) : localInterp);

            usedSamples = interp.process (speedRatio,
                                          sourceBuffer.getReadPointer (ch),
                                          resampledBuffer.getWritePointer (ch),
                                          numDestSamples);
        }

        if (resampleState != nullptr)
            resampleState->advanceSourceSamples (usedSamples);

        // 4. チャンネル変換して出力
        writeToDest (destBuffer, destStartSample, numDestSamples, resampledBuffer, options.addToDestBuffer);

        return complete;
    };

    if (sourceChannels <= maxStackChannels && numSourceSamples <= maxStackSamples && numDestSamples <= maxStackSamples)
    {
        for (int ch = 0; ch < sourceChannels; ++ch)
        {
            stackSourcePointers[ch] = stackSourceMemory + ch * numSourceSamples;
            stackResampledPointers[ch] = stackResampledMemory + ch * numDestSamples;
        }

        juce::AudioBuffer<float> stackSourceBuffer (stackSourcePointers, sourceChannels, numSourceSamples);
        juce::AudioBuffer<float> stackResampledBuffer (stackResampledPointers, sourceChannels, numDestSamples);
        return runResampled (stackSourceBuffer, stackResampledBuffer);
    }

    juce::AudioBuffer<float> heapSourceBuffer (sourceChannels, numSourceSamples);
    juce::AudioBuffer<float> heapResampledBuffer (sourceChannels, numDestSamples);
    return runResampled (heapSourceBuffer, heapResampledBuffer);
}

} // namespace gliss
