#include "StretchReader.h"

#include <algorithm>
#include <cmath>

namespace gliss
{

void StretchReader::prepare (double hostRateIn, int maxBlockSize, int channels, double sourceRateIn)
{
    hostRate = hostRateIn;
    sourceRate = sourceRateIn;
    channelCount = juce::jlimit (1, RegionReader::maxChannels, channels);
    const auto block = std::max (1, maxBlockSize);
    inputCapacity = 8 * block + 8;
    reader.prepare (hostRate, std::max (inputCapacity, 1), channelCount, sourceRate);
    stretch.presetDefault (channelCount, (float) hostRate);
    inputWork.setSize (channelCount, inputCapacity);
    seekWork.setSize (channelCount, stretch.inputLatency() + 8 * stretch.outputLatency() + 8);
    outputWork.setSize (channelCount, block);
    primed = false;
}

bool StretchReader::readInput (juce::AudioBuffer<float>& work, int count, double hostPosition,
                               SourceReader* source, const EditedPcm* pcm, const RegionReadOptions& options) noexcept
{
    const auto sourcePosition = (juce::int64) std::floor (hostPosition * sourceRate / hostRate + 0.5);
    RegionReadOptions readOptions = options;
    readOptions.addToDestBuffer = false;
    return reader.readBlock (work, 0, count, sourcePosition, source, pcm, readOptions);
}

bool StretchReader::readBlock (juce::AudioBuffer<float>& dest, int destStart, int count,
                               juce::int64 playbackSample, double modificationSample, double scale,
                               SourceReader* source, const EditedPcm* pcm, const RegionReadOptions& options) noexcept
{
    if (count <= 0 || hostRate <= 0.0 || scale <= 0.0 || dest.getNumChannels() != channelCount
        || inputCapacity <= 0 || scale > inputCapacity - 2)
        return false;

    bool complete = true;
    if (! primed || playbackSample != nextPlayback || ! juce::exactlyEqual (scale, currentScale)
        || options.compareMode != currentCompareMode
        || std::abs (modificationSample - (originModification + (double) outputCount * scale)) > 2.0)
    {
        // outputSeek に現在位置から先の入力を渡す。次の process はその直後の入力から始める。
        const auto seekLength = juce::jmin (seekWork.getNumSamples(), stretch.outputSeekLength ((float) scale));
        complete = readInput (seekWork, seekLength, modificationSample,
                              source, pcm, options) && complete;
        stretch.outputSeek (seekWork.getArrayOfReadPointers(), seekLength);
        originModification = modificationSample;
        outputCount = 0;
        seekInputLength = seekLength;
        inputCursor = (juce::int64) std::floor (modificationSample + seekLength + 0.5);
        currentScale = scale;
        currentCompareMode = options.compareMode;
        primed = true;
    }

    int written = 0;
    while (written < count)
    {
        const auto maxOutput = std::max (1, (int) ((inputCapacity - 2) / scale));
        const auto chunk = std::min ({ count - written, outputWork.getNumSamples(), maxOutput });
        const auto targetInput = (juce::int64) std::floor (originModification + seekInputLength + (double) (outputCount + chunk) * scale + 0.5);
        const auto inputCount = (int) (targetInput - inputCursor);
        if (inputCount < 0 || inputCount > inputCapacity)
            return false;

        complete = readInput (inputWork, inputCount, (double) inputCursor, source, pcm, options) && complete;
        stretch.process (inputWork.getArrayOfReadPointers(), inputCount, outputWork.getArrayOfWritePointers(), chunk);
        for (int ch = 0; ch < channelCount; ++ch)
        {
            if (options.addToDestBuffer)
                dest.addFrom (ch, destStart + written, outputWork, ch, 0, chunk);
            else
                dest.copyFrom (ch, destStart + written, outputWork, ch, 0, chunk);
        }
        written += chunk;
        outputCount += chunk;
        inputCursor = targetInput;
    }
    nextPlayback = playbackSample + count;
    return complete;
}

} // namespace gliss
