#include "SourceReader.h"

#include <algorithm>

namespace gliss
{

//==============================================================================
FormatSourceReader::FormatSourceReader (std::unique_ptr<juce::AudioFormatReader> readerIn,
                                       juce::BufferingAudioReader* bufferingReaderIn)
    : reader (std::move (readerIn)),
      bufferingReader (bufferingReaderIn)
{
}

double FormatSourceReader::getSampleRate() const noexcept
{
    return reader != nullptr ? reader->sampleRate : 0.0;
}

int FormatSourceReader::getNumChannels() const noexcept
{
    return reader != nullptr ? (int) reader->numChannels : 0;
}

juce::int64 FormatSourceReader::getLengthInSamples() const noexcept
{
    return reader != nullptr ? reader->lengthInSamples : 0;
}

bool FormatSourceReader::readSourceSamples (juce::AudioBuffer<float>& destBuffer,
                                            int destStartSample,
                                            int numSamples,
                                            juce::int64 startInSource,
                                            int timeoutMs) noexcept
{
    if (reader == nullptr || numSamples <= 0)
    {
        for (int ch = 0; ch < destBuffer.getNumChannels(); ++ch)
            destBuffer.clear (ch, destStartSample, numSamples);
        return false;
    }

    if (bufferingReader != nullptr)
        bufferingReader->setReadTimeout (timeoutMs);

    // AudioFormatReader::read は destBuffer に指定サンプル数読み込む
    // 未到達・不足部分は通常ゼロで埋められる
    const auto complete = reader->read (&destBuffer, destStartSample, numSamples, startInSource, true, true);
    return complete;
}

//==============================================================================
BufferSourceReader::BufferSourceReader (const juce::AudioBuffer<float>& sourceBuffer, double sampleRateIn)
    : buffer (sourceBuffer),
      sampleRate (sampleRateIn)
{
}

BufferSourceReader::BufferSourceReader (juce::AudioBuffer<float>&& sourceBuffer, double sampleRateIn)
    : buffer (std::move (sourceBuffer)),
      sampleRate (sampleRateIn)
{
}

bool BufferSourceReader::readSourceSamples (juce::AudioBuffer<float>& destBuffer,
                                            int destStartSample,
                                            int numSamples,
                                            juce::int64 startInSource,
                                            int /*timeoutMs*/) noexcept
{
    if (numSamples <= 0)
        return true;

    const auto totalSrc = (juce::int64) buffer.getNumSamples();
    const auto srcChans = buffer.getNumChannels();
    const auto destChans = destBuffer.getNumChannels();

    // 読み出し区間 [startInSource, startInSource + numSamples) と [0, totalSrc) の交差
    const auto validStartInSource = std::max ((juce::int64) 0, startInSource);
    const auto validEndInSource = std::min (totalSrc, startInSource + (juce::int64) numSamples);

    if (validStartInSource >= validEndInSource)
    {
        // 全く重ならない
        for (int ch = 0; ch < destChans; ++ch)
            destBuffer.clear (ch, destStartSample, numSamples);
        return false;
    }

    const auto offsetInDest = (int) (validStartInSource - startInSource);
    const auto validCount = (int) (validEndInSource - validStartInSource);

    // 前後の範囲外をゼロクリア
    if (offsetInDest > 0)
    {
        for (int ch = 0; ch < destChans; ++ch)
            destBuffer.clear (ch, destStartSample, offsetInDest);
    }

    const auto trailingZeroCount = numSamples - (offsetInDest + validCount);
    if (trailingZeroCount > 0)
    {
        for (int ch = 0; ch < destChans; ++ch)
            destBuffer.clear (ch, destStartSample + offsetInDest + validCount, trailingZeroCount);
    }

    // 有効範囲をコピー
    const auto copyChannels = std::min (srcChans, destChans);
    for (int ch = 0; ch < copyChannels; ++ch)
    {
        destBuffer.copyFrom (ch, destStartSample + offsetInDest,
                             buffer, ch, (int) validStartInSource, validCount);
    }

    // もし destChannels > srcChannels の場合、残りのチャンネルをゼロクリア
    for (int ch = copyChannels; ch < destChans; ++ch)
    {
        destBuffer.clear (ch, destStartSample + offsetInDest, validCount);
    }

    return (validCount == numSamples);
}

} // namespace gliss
