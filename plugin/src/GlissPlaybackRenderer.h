#pragma once

#include <juce_audio_formats/juce_audio_formats.h>
#include <juce_audio_processors/juce_audio_processors.h>

#include "GlissDocumentController.h"

#include <atomic>
#include <map>
#include <memory>
#include <vector>

namespace gliss
{

/** ホストの音を先読みして返す、素通しの PlaybackRenderer。

    - ホストの音（ARAAudioSourceReader）は裏のスレッド（全インスタンスで 1 本）が先読みしてバッファに溜める
      （BufferingAudioReader）。オーディオスレッドはそのバッファを写すだけで、ホストの音の読み出しをしない。
      取るのは、先読みスレッドがバッファを入れ替える瞬間だけ持つ短いロックだけ。
    - 先読みが間に合っていない範囲は、読めたところまでを返し、残りだけを無音にする（全部を無音にしない）。
    - ホストが「リアルタイムでない」と言っている描画（バウンスなど）のときだけ、先読みの完了を短く待つ。
      常にリアルタイムでないインスタンス（alwaysNonRealtime）は、先読みせず直に読む。
    - ソースとホストのサンプリング周波数が違うものは、まだ鳴らさない（段階 2 でエンジンの soxr に任せる）。
*/
class GlissPlaybackRenderer final : public juce::ARAPlaybackRenderer
{
public:
    GlissPlaybackRenderer (ARA::PlugIn::DocumentController* documentController, ProcessingLockInterface& lockInterface);
    ~GlissPlaybackRenderer() override;

    void prepareToPlay (double sampleRate,
                        int maximumSamplesPerBlock,
                        int numChannels,
                        juce::AudioProcessor::ProcessingPrecision precision,
                        AlwaysNonRealtime alwaysNonRealtime) override;
    void releaseResources() override;

    bool processBlock (juce::AudioBuffer<float>& buffer,
                       juce::AudioProcessor::Realtime realtime,
                       const juce::AudioPlayHead::PositionInfo& positionInfo) noexcept override;

    using ARAPlaybackRenderer::processBlock;

private:
    /** 1 つの AudioSource を読む窓口。リージョンの追加・削除は prepareToPlay の外でしか起きない（ARA の規則）ので、
        この表は prepareToPlay / releaseResources でだけ作り替える。 */
    struct SourceReader
    {
        std::unique_ptr<juce::AudioFormatReader> reader;           // 先読みつき（BufferingAudioReader）か、直に読む ARAAudioSourceReader
        juce::BufferingAudioReader* buffered = nullptr;            // reader が先読みつきのとき、待ち時間を変えるための別名
        bool supported = false;                                    // サンプリング周波数が合っている
    };

    /** 検証用の記録（GLISS_ARA_TRACE_DIR のときだけ使う）。1 回の region の読みごとに 1 件。 */
    struct TraceEntry
    {
        juce::int64 timeInSamples = 0;
        juce::int64 startInSource = 0;
        int numSamples = 0;
        bool complete = false;
        double sum = 0.0;
        double sumSquares = 0.0;
    };

    /** 全インスタンスで 1 本の先読みスレッド（ホストの音の読み出しはここでだけ行う）。 */
    class PrefetchThread final : public juce::TimeSliceThread
    {
    public:
        PrefetchThread() : juce::TimeSliceThread ("Gliss ARA prefetch") { startThread (juce::Thread::Priority::high); }
        ~PrefetchThread() override { stopThread (2000); }
    };

    void dumpTrace();

    ProcessingLockInterface& lockInterface;
    juce::SharedResourcePointer<PrefetchThread> prefetchThread;   // sourceReaders より先に作り、後に壊す

    double sampleRate = 48000.0;
    int maximumSamplesPerBlock = 0;
    int numChannels = 2;
    bool prepared = false;
    int forcedTimeoutMs = -1;            // GLISS_ARA_READ_TIMEOUT_MS（検証用）
    bool tracing = false;                // prepareToPlay で GLISS_ARA_TRACE_DIR を見て決める

    std::map<juce::ARAAudioSource*, SourceReader> sourceReaders;
    juce::AudioBuffer<float> scratch;

    // 統計（オーディオスレッドが書く。読むのは releaseResources だけ）
    std::atomic<juce::int64> blocksRendered { 0 }, samplesRendered { 0 }, incompleteReads { 0 }, unsupportedRegions { 0 }, lockMisses { 0 };

    std::vector<TraceEntry> trace;
    std::atomic<size_t> traceCount { 0 };
};

} // namespace gliss
