#pragma once

#include <juce_audio_formats/juce_audio_formats.h>
#include <juce_audio_processors/juce_audio_processors.h>

#include "GlissDocumentController.h"
#include "cache/RegionReader.h"
#include "cache/SourceReader.h"
#include "cache/StretchReader.h"

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
    - 原音のリーダーは、ホストがソースのサンプルを変えた・サンプルへのアクセスを戻したとき（`refreshSource`。ARAAudioSourceReader は
      一度無効になると戻らず、BufferingAudioReader は読めなかった区間を無音のまま持つ）、メッセージスレッドが作り直して差し替える。
      古いリーダーは、オーディオスレッドが使い終わるまで（ブロックが進むまで）壊さない。
    - リージョンの追加・削除（`didAddPlaybackRegion`・`willRemovePlaybackRegion`）も、メッセージスレッドが表を作り直して差し替える。
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

    /** メッセージスレッド（ARA のメインスレッド）から呼ぶ。ソースの原音のリーダーを作り直して差し替える（準備前なら何もしない）。
        reason: ログ用（"content"・"samples-access"・"properties"）。形式（周波数・チャンネル数）が変わっていれば、そのリージョンの読み出しも作り直す。 */
    void refreshSource (juce::ARAAudioSource* source, const char* reason);

    /** メッセージスレッドから呼ぶ。ホストが持つリージョンのうち表に無いもの（準備のときに読み出しを作れなかった・準備の後に増えた）に読み出しを作り、無くなったものを外す。 */
    void syncRegions();

    bool processBlock (juce::AudioBuffer<float>& buffer,
                       juce::AudioProcessor::Realtime realtime,
                       const juce::AudioPlayHead::PositionInfo& positionInfo) noexcept override;

    using ARAPlaybackRenderer::processBlock;

private:
    /** リージョンごとの読み出し。リージョンの追加・削除は prepareToPlay の外でしか起きない（ARA の規則）ので、
        表は prepareToPlay / releaseResources でだけ作り替える。 */
    struct SourceSlot;
    struct RegionEntry
    {
        RegionReader reader;
        StretchReader stretch;
        SourceSlot* slot = nullptr;                // sources が持つ。原音のリーダーは slot->current（差し替わる）
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

protected:
    void didAddPlaybackRegion (ARA::PlugIn::PlaybackRegion* region) noexcept override;
    void willRemovePlaybackRegion (ARA::PlugIn::PlaybackRegion* region) noexcept override;

private:
    /** ソースごとの原音のリーダー。メッセージスレッドが差し替え、オーディオスレッドは current を読むだけ。 */
    struct SourceSlot
    {
        std::atomic<FormatSourceReader*> current { nullptr };
        std::unique_ptr<FormatSourceReader> owned;       // current の持ち主（メッセージスレッドだけが触る）
        juce::String id;                                 // ARAAudioSource の persistentID（ログ用）
        double rate = 0.0;
        int channels = 0;
        juce::int64 length = 0;
    };

    /** オーディオスレッドが読む、リージョン → 読み出しの表。作ったら変えず、差し替えるときは新しい表を作る。 */
    struct RegionTable
    {
        std::vector<std::pair<const juce::ARAPlaybackRegion*, RegionEntry*>> items;
    };

    /** 差し替えた古いもの。差し替えの時点で始まっていたブロックがすべて終わる（blocksExited が block 以上になる）まで壊さない。 */
    struct Retired
    {
        std::unique_ptr<const RegionTable> table;
        std::unique_ptr<RegionEntry> entry;
        std::unique_ptr<FormatSourceReader> reader;
        juce::int64 block = 0;
    };

    std::unique_ptr<FormatSourceReader> makeSourceReader (juce::ARAAudioSource* source) const;
    SourceSlot* slotFor (juce::ARAAudioSource* source);
    std::unique_ptr<RegionEntry> makeEntry (const juce::ARAPlaybackRegion* region);
    void publishTable();
    void retire (Retired&& item);
    bool hasEntry (const juce::ARAPlaybackRegion* region) const;
    void collectRetired();
    void logAggregate (const juce::String& line);

    void waitForSync (bool offline) noexcept;
    void dumpTrace();

    RenderContext& context;
    juce::SharedResourcePointer<PrefetchThread> prefetchThread;   // sourceReaders より先に作り、後に壊す

    double sampleRate = 48000.0;
    int maximumSamplesPerBlock = 0;
    int numChannels = 2;
    std::atomic<bool> prepared { false };
    bool readDirectly = false;           // prepareToPlay で決める（原音のリーダーを作り直すときも同じ）
    int forcedTimeoutMs = -1;            // GLISS_ARA_READ_TIMEOUT_MS（検証用）
    int forcedSyncWaitMs = -1;           // GLISS_ARA_SYNC_WAIT_MS（検証用。リアルタイムでも同期を待つ）
    bool tracing = false;                // prepareToPlay で GLISS_ARA_TRACE_DIR を見て決める

    // メッセージスレッドだけが触る持ち主
    std::map<juce::ARAAudioSource*, std::unique_ptr<SourceSlot>> sources;
    std::vector<std::pair<const juce::ARAPlaybackRegion*, std::unique_ptr<RegionEntry>>> entries;
    std::vector<Retired> retired;
    int skippedRegions = 0;              // 読み出しを作れなかったリージョンの数（形式が読めない・修飾が無い）
    int readerRefreshes = 0;
    // オーディオスレッドが読む表（差し替えは publishTable。古い表は retired へ）
    std::atomic<const RegionTable*> table { nullptr };

    double syncWaitBudgetMs = 0.0;       // この prepareToPlay の間に同期を待ってよい残り（オーディオスレッドだけが書く）

    // 統計（オーディオスレッドが書く。読むのは releaseResources だけ）
    std::atomic<juce::int64> blocksRendered { 0 }, samplesRendered { 0 }, incompleteReads { 0 }, lockMisses { 0 },
                             syncWaitMs { 0 }, syncWaitTimeouts { 0 }, unpreparedBlocks { 0 }, noSourceReads { 0 }, unknownRegionBlocks { 0 };

    // 差し替えた古いものを壊してよい時を知るための、ブロックの出入りの数（processBlock の頭と、すべての出口で増やす）
    std::atomic<juce::int64> blocksEntered { 0 }, blocksExited { 0 };

    std::vector<TraceEntry> trace;
    std::atomic<size_t> traceCount { 0 };
};

} // namespace gliss
