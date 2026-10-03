#pragma once

#include <juce_audio_formats/juce_audio_formats.h>
#include <juce_audio_processors/juce_audio_processors.h>

#include "GlissDocumentController.h"
#include "cache/RegionReader.h"
#include "cache/SourceReader.h"

#include <atomic>
#include <map>
#include <memory>
#include <vector>

namespace gliss
{

/** 編集を当てた音を返す PlaybackRenderer（docs/ara-plugin.md の「再生」）。

    - オーディオスレッドはキャッシュ（AudioModification ごとの EditedPcm のスナップショット）を読むだけ。
      IPC・再合成・ホストの音の読み出し・確保・ロック待ちをしない。キャッシュの無い区間は原音。
    - 原音は段階 1 と同じく裏のスレッド（全インスタンスで 1 本）がホストの音を先読みしたもの（BufferingAudioReader）。
      先読みが間に合っていない範囲だけ無音。
    - リージョンごとに RegionReader（窓＋原音＋周波数の変換＋チャンネル数の変換）を prepareToPlay で用意する。
      ソースとホストの周波数が違っても鳴らす（流しの変換）。
    - 非リアルタイムの描画（バウンス）のときだけ、同期（ソースの読み込み・エンジン・差分の再合成）の完了を最大 10 秒待つ
      （prepareToPlay ごとの持ち時間。待っても済まなければ原音のまま描いてログに書く）。
    - 原音と比べる（setCompare）間は窓を当てない。 */
class GlissPlaybackRenderer final : public juce::ARAPlaybackRenderer
{
public:
    GlissPlaybackRenderer (ARA::PlugIn::DocumentController* documentController, RenderContext& context);
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
    /** リージョンごとの読み出し。リージョンの追加・削除は prepareToPlay の外でしか起きない（ARA の規則）ので、
        表は prepareToPlay / releaseResources でだけ作り替える。 */
    struct RegionEntry
    {
        RegionReader reader;
        FormatSourceReader* source = nullptr;      // sourceReaders が持つ
        std::shared_ptr<EditedPcm> pcm;            // 修飾と持ち合う（オーディオスレッドでは参照の数を変えない）
        double sourceRate = 0.0;
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

    void waitForSync (bool offline) noexcept;
    void dumpTrace();

    RenderContext& context;
    juce::SharedResourcePointer<PrefetchThread> prefetchThread;   // sourceReaders より先に作り、後に壊す

    double sampleRate = 48000.0;
    int maximumSamplesPerBlock = 0;
    int numChannels = 2;
    bool prepared = false;
    int forcedTimeoutMs = -1;            // GLISS_ARA_READ_TIMEOUT_MS（検証用）
    int forcedSyncWaitMs = -1;           // GLISS_ARA_SYNC_WAIT_MS（検証用。リアルタイムでも同期を待つ）
    bool tracing = false;                // prepareToPlay で GLISS_ARA_TRACE_DIR を見て決める

    std::map<juce::ARAAudioSource*, std::unique_ptr<FormatSourceReader>> sourceReaders;
    std::map<const juce::ARAPlaybackRegion*, std::unique_ptr<RegionEntry>> regionEntries;

    double syncWaitBudgetMs = 0.0;       // この prepareToPlay の間に同期を待ってよい残り（オーディオスレッドだけが書く）

    // 統計（オーディオスレッドが書く。読むのは releaseResources だけ）
    std::atomic<juce::int64> blocksRendered { 0 }, samplesRendered { 0 }, incompleteReads { 0 }, lockMisses { 0 },
                             syncWaitMs { 0 }, syncWaitTimeouts { 0 };

    std::vector<TraceEntry> trace;
    std::atomic<size_t> traceCount { 0 };
};

} // namespace gliss
