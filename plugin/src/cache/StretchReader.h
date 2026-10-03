#pragma once

#include "RegionReader.h"
#include <signalsmith-stretch/signalsmith-stretch.h>

namespace gliss
{

/** 修飾の音をホストの周波数で読み、ピッチを保ってソングの時間へ伸縮する。 */
class StretchReader
{
public:
    void prepare (double hostRate, int maxBlockSize, int channels, double sourceRate);

    /** playbackSample はソング上、modificationSample は修飾上の位置（どちらもホスト周波数のサンプル）。 */
    bool readBlock (juce::AudioBuffer<float>& dest, int destStart, int count,
                    juce::int64 playbackSample, double modificationSample, double scale,
                    SourceReader* source, const EditedPcm* pcm, const RegionReadOptions& options = {}) noexcept;

private:
    bool readInput (juce::AudioBuffer<float>& work, int count, double hostPosition,
                    SourceReader* source, const EditedPcm* pcm, const RegionReadOptions& options) noexcept;

    RegionReader reader;
    signalsmith::stretch::SignalsmithStretch<float> stretch;
    juce::AudioBuffer<float> inputWork, seekWork, outputWork;
    double hostRate = 0.0, sourceRate = 0.0;
    double currentScale = 0.0, originModification = 0.0;
    juce::int64 nextPlayback = 0;
    juce::int64 inputCursor = 0;
    int seekInputLength = 0;
    juce::int64 outputCount = 0;
    int inputCapacity = 0, channelCount = 0;
    bool primed = false;
    bool currentCompareMode = false;
};

} // namespace gliss
