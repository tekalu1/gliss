#pragma once

#include <juce_core/juce_core.h>

#include <memory>

namespace gliss
{

/** ソースの音をエンジンに渡す float32 の WAV（WAVE_FORMAT_IEEE_FLOAT、インターリーブ）を書く。
    ホストから読んだ float をそのまま書く（変換しない）ので、エンジンが読む値はホストの値と同じ。
    書き終えたら finish() でヘッダの長さを埋める。 */
class FloatWavWriter
{
public:
    FloatWavWriter() = default;
    ~FloatWavWriter();

    bool open (const juce::File&, double sampleRate, int numChannels);

    /** チャンネルごとの列（numChannels 本）から numSamples 個を書く。 */
    bool write (const float* const* channels, int numSamples);

    bool finish();

    juce::int64 getNumSamplesWritten() const noexcept { return written; }

private:
    bool writeHeader (juce::int64 numFrames);

    std::unique_ptr<juce::FileOutputStream> stream;
    int numChannels = 0;
    double sampleRate = 0.0;
    juce::int64 written = 0;
    juce::HeapBlock<float> interleaved;
    int interleavedCapacity = 0;
};

} // namespace gliss
