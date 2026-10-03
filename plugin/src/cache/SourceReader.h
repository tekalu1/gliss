#pragma once

#include <juce_audio_basics/juce_audio_basics.h>
#include <juce_audio_formats/juce_audio_formats.h>
#include <juce_core/juce_core.h>

#include <memory>

namespace gliss
{

/** 原音（AudioSource）の PCM 読み出しインターフェース。
    オーディオスレッドまたはレンダラーから呼ばれ、足りない・読めない区間は無音（ゼロ）で返す。 */
class SourceReader
{
public:
    virtual ~SourceReader() = default;

    virtual double getSampleRate() const noexcept = 0;
    virtual int getNumChannels() const noexcept = 0;
    virtual juce::int64 getLengthInSamples() const noexcept = 0;

    /** 指定位置から原音サンプルを読み出し、destBuffer の destStartSample から格納する。
        読めなかった（足りない、先読み未完了など）部分はゼロクリアする。
        @param destBuffer       読み出し先バッファ
        @param destStartSample  書き込み開始インデックス
        @param numSamples       読み出すサンプル数
        @param startInSource    ソースの時間軸（サンプル単位）での開始位置
        @param timeoutMs        先読みの待ち時間（ミリ秒、リアルタイム時は 0）
        @return 完全に全サンプル読めたら true、一部でも欠けていれば false
    */
    virtual bool readSourceSamples (juce::AudioBuffer<float>& destBuffer,
                                    int destStartSample,
                                    int numSamples,
                                    juce::int64 startInSource,
                                    int timeoutMs = 0) noexcept = 0;
};

/** juce::AudioFormatReader（BufferingAudioReader 含む）をラップする実装。 */
class FormatSourceReader final : public SourceReader
{
public:
    explicit FormatSourceReader (std::unique_ptr<juce::AudioFormatReader> readerIn,
                                 juce::BufferingAudioReader* bufferingReaderIn = nullptr);
    ~FormatSourceReader() override = default;

    double getSampleRate() const noexcept override;
    int getNumChannels() const noexcept override;
    juce::int64 getLengthInSamples() const noexcept override;

    bool readSourceSamples (juce::AudioBuffer<float>& destBuffer,
                            int destStartSample,
                            int numSamples,
                            juce::int64 startInSource,
                            int timeoutMs = 0) noexcept override;

    juce::AudioFormatReader* getReader() const noexcept { return reader.get(); }
    juce::BufferingAudioReader* getBufferingReader() const noexcept { return bufferingReader; }

private:
    std::unique_ptr<juce::AudioFormatReader> reader;
    juce::BufferingAudioReader* bufferingReader = nullptr;
};

/** メモリ上の AudioBuffer<float> をラップする実装（テスト用・メモリ音源用）。 */
class BufferSourceReader final : public SourceReader
{
public:
    BufferSourceReader (const juce::AudioBuffer<float>& sourceBuffer, double sampleRateIn);
    BufferSourceReader (juce::AudioBuffer<float>&& sourceBuffer, double sampleRateIn);
    ~BufferSourceReader() override = default;

    double getSampleRate() const noexcept override { return sampleRate; }
    int getNumChannels() const noexcept override { return buffer.getNumChannels(); }
    juce::int64 getLengthInSamples() const noexcept override { return buffer.getNumSamples(); }

    bool readSourceSamples (juce::AudioBuffer<float>& destBuffer,
                            int destStartSample,
                            int numSamples,
                            juce::int64 startInSource,
                            int timeoutMs = 0) noexcept override;

    const juce::AudioBuffer<float>& getBuffer() const noexcept { return buffer; }

private:
    juce::AudioBuffer<float> buffer;
    double sampleRate = 48000.0;
};

} // namespace gliss
